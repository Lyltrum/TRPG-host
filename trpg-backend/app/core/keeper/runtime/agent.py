"""KeeperAgent v2：两阶段回合制的 COC 守秘人（裁决与叙事分离，两段式玩家掷骰）。

对外它仍只是一个 `Narrator`——WS 层照常 `narrate(context) -> NarrationOutcome`，
协议/锁/广播零改动；改造全部发生在 narrate()/resolve_check() 内部。

v1（openai-agents 自由工具调用）为什么被推翻：一次 LLM 调用同时承担
理解/裁决/记账/叙事，模型的写作本能碾压其余三件，实测四类 bug 同一病灶
（该掷不掷、线索白给、状态不记、骰值藏进叙事），三轮 prompt 强化 + 两次
结构强制都只是补丁。v2 仿真人 KP 的台前/幕后分离：

    action.submit
      ↓ 阶段1·裁决（LLM，JSON mode，低温）→ KeeperDecision
      ↓ 阶段2·执行（纯代码）           → HP/状态立即写库；检定进 pending 队列
      ↓ 阶段3·叙事（LLM，只写故事）    → 广播文本 + 待掷检定通知

    玩家点击「掷骰」→ resolve_check → 服务端权威掷骰/写库/留痕
      → 队列还有 → 只广播这次结果，等下一次掷骰
      → 队列清空 → 复用 narrate() 结算叙事（裁决器能看到刚掷出的结果）

两段式玩家掷骰：骰子不再由裁决/叙事阶段直接摇出，而是由玩家在前端点击
「掷骰」确认后，服务端权威生成骰值——`pending.py` 的进程内队列是"裁决已
判定需要检定"与"骰子真正掷出"之间的缓冲区。

openai-agents SDK 不再出现在这条主路径上（依赖暂保留，未来多 agent 实验
可能复用）。
"""

import asyncio
import random
from dataclasses import replace

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.keeper.access.leak_guard import log_leak_hits, scrub_meta_leaks
from app.core.keeper.access.subject import KEEPER
from app.core.keeper.capabilities import (
    audit_fields,
    reserved_state_keys,
    settler_for,
)
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.contract.registry import Capability
from app.core.keeper.memory.chapter import (
    record_chapter,
    should_summarize,
    turns_since_last_chapter,
)
from app.core.keeper.memory.fact_ledger import (
    record_revelations,
    render_ledger,
    visible_fact_ids,
)
from app.core.keeper.memory.history import (
    HISTORY_EVENT_TYPES,
    HISTORY_LIMIT,
    HistoryLine,
    history_lines_from_events,
)
from app.core.keeper.narration.narration_hints import (
    NO_PENDING_CHECK_HINT,
    UNRESOLVED_CONFLICT_HINT,
    build_bystander_hint,
    build_check_boundary_hint,
)
from app.core.keeper.narration.prompts import (
    build_adjudicator_instructions,
    build_narrator_instructions,
)
from app.core.keeper.narration.prose_discipline import (
    clip_narration,
    inject_scene_transition_guidance,
    narration_limit,
    narration_max_tokens,
    scrub_kp_anti_patterns,
)
from app.core.keeper.narration.sheet_digest import format_sheet
from app.core.keeper.narration.situation import SituationBuilder, build_situation
from app.core.keeper.primitives.dice import is_success
from app.core.keeper.runtime.decision_log import record_decision
from app.core.keeper.runtime.deps import KeeperDeps, KeeperToolError
from app.core.keeper.runtime.llm_calls import (
    FALLBACK_ADJUDICATE_GUIDANCE,
    REQUEST_TIMEOUT_SECONDS,
    adjudicate,
    narrate_prose,
    narrate_prose_stream,
    summarize_chapter,
)
from app.core.keeper.runtime.location_state import (
    group_players,
    load_hidden_players,
    location_of,
)
from app.core.keeper.runtime.location_state import (
    scene_changed as has_scene_changed,
)
from app.core.keeper.runtime.narration_stream import NarrationStream
from app.core.keeper.runtime.pending import pending_check_manager, to_notice
from app.core.keeper.runtime.phase import (
    PHASE_FINISHED,
    PHASE_KEY,
    PHASE_OPENING,
    load_ending_id,
    load_phase,
    set_phase_impl,
)
from app.core.keeper.runtime.turn_executor import create_pending_checks, execute_side_effects
from app.core.keeper.runtime.turn_policy import (
    CHECK_CAPABILITIES,
    apply_code_forcing,
    classify_turn,
)
from app.core.llm_tape import build_llm_client
from app.core.narration.contract import (
    CheckResultCallback,
    NarrationContext,
    NarrationDeltaSink,
    NarrationOutcome,
    NarrationSegment,
    Narrator,
    PlayerUtterance,
)
from app.core.narration.deepseek import DEEPSEEK_BASE_URL
from app.dto.game import RulesetRead
from app.models.event import Event
from app.models.room import Character, Player, Room

logger = structlog.get_logger()


class KeeperAgent(Narrator):
    def __init__(
        self,
        api_key: str,
        module: ScenarioModule,
        ruleset: RulesetRead,
        session_factory: async_sessionmaker[AsyncSession],
        rng: random.Random | None = None,
    ) -> None:
        self._module = module
        self._ruleset = ruleset
        self._session_factory = session_factory
        self._rng = rng if rng is not None else random.Random()
        self._client = build_llm_client(
            api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS
        )
        self._background: set[asyncio.Task] = set()
        self._adjudicator_instructions = build_adjudicator_instructions(module, ruleset)
        self._narrator_instructions = build_narrator_instructions(module)

    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        if context.room_id is None or context.player_id is None:
            raise ValueError("KeeperAgent 需要 NarrationContext 携带 room_id/player_id")
        room_id = context.room_id
        is_heartbeat = getattr(context, "is_heartbeat", False)
        is_opening_ceremony = getattr(context, "is_opening_ceremony", False)
        # 本轮一起发言的人（收集窗口合并的那一批）。单人局 = (发起者,)，
        # 与 P5.2 之前逐字一致。
        turn_player_ids = tuple(context.participant_ids) or (context.player_id,)

        # 两段式玩家掷骰：还有待掷的检定时不再裁决新一轮——先让玩家把手头的
        # 骰子掷完。重发同一个请求（而不是静默不回应），防前端刷新丢卡片。
        # 主动心跳 / 开场仪式：有待掷时直接放弃（开场不该卡在旧检定上）。
        async with self._session_factory() as db:
            pending = await pending_check_manager.first(db, room_id)
        if pending is not None:
            if is_heartbeat or is_opening_ceremony:
                return NarrationOutcome(text="")
            logger.info(
                "keeper_narrate_pending_guard",
                room_id=room_id,
                check_request_id=pending.check_request_id,
            )
            return NarrationOutcome(
                text="守秘人正在等待掷骰——请先完成待掷的检定。",
                check_requests=[to_notice(pending)],
            )

        keeper_state, history_lines, roster, players = await self._load_room_memory(room_id)

        # 对局已结束：拒绝新行动（心跳亦静默）
        phase = load_phase(keeper_state)
        ending_id = load_ending_id(keeper_state)
        if phase == PHASE_FINISHED:
            if is_heartbeat or is_opening_ceremony:
                return NarrationOutcome(text="")
            return NarrationOutcome(
                text=f"本局已结束（结局：{ending_id or '—'}）。感谢各位调查员。"
            )

        # 开场仪式或首次进入：模组有 opening 且尚未记阶段 → 初始化为 opening
        # （设计 05：game.start 后第一轮即开场仪式，不干等玩家）
        if phase is None and (is_opening_ceremony or self._module.opening is not None):
            deps_boot = KeeperDeps(
                room_id=room_id,
                player_id=context.player_id,
                session_factory=self._session_factory,
                module=self._module,
                ruleset=self._ruleset,
                reserved_state_keys=reserved_state_keys(),
                turn_player_ids=turn_player_ids,
                rng=self._rng,
            )
            await set_phase_impl(deps_boot, PHASE_OPENING)
            phase = PHASE_OPENING
            keeper_state = {
                **(keeper_state or {}),
                PHASE_KEY: PHASE_OPENING,
            }

        situation_builder = await build_situation(
            session_factory=self._session_factory,
            module=self._module,
            room_id=room_id,
            observer_id=context.player_id,
            keeper_state=keeper_state,
            history_lines=history_lines,
            roster=roster,
            players=players,
            phase=phase,
            ending_id=ending_id,
            is_heartbeat=is_heartbeat,
            is_opening_ceremony=is_opening_ceremony,
        )
        situation = situation_builder.for_keeper(
            nickname=context.player_nickname, utterance=context.utterance
        )

        # 有 structured 开场素材时：仪式轮不跑裁决（开场没有玩家行动可裁决），
        # 直接把素材喂给叙事阶段的 LLM 改写——不再原样播报。
        # 🔴 真人实测 2026-07-28（神秘渡轮）：原样播报等于让 AI 主持人
        # "照本宣科"念模组书的背景说明，模组数据里混进的 GM 指导语等缺陷
        # 也会被原样带进游戏（例句："让PC通过表演而使得……活跃跑团的气氛"，
        # 这是写给守秘人看的素材，不是能念给玩家听的台词）。走跟其他每一轮
        # 完全一样的"KP 声音"处理后，这类缺陷会被叙事语气自然冲掉。
        if is_opening_ceremony:
            opening_material = ""
            if self._module.opening and (self._module.opening.script or "").strip():
                opening_material = self._module.opening.script.strip()
            elif (self._module.player_intro or "").strip():
                opening_material = self._module.player_intro.strip()
            if opening_material:
                char_limit = narration_limit(is_opening_ceremony=True)
                token_limit = narration_max_tokens(char_limit)
                opening_decision = KeeperDecision(
                    thinking="开场仪式：把 structured 开场素材改写成守秘人的开场白。",
                    narration_guidance=(
                        "下面是本模组的开场素材（背景说明，写给守秘人看的，不是可以"
                        "逐字照抄的台词）：\n"
                        f"{opening_material}\n\n"
                        "请用你自己的话、以守秘人叙事口吻讲给玩家听——建立场景与处境，"
                        "让调查员知道身在何处；禁止逐字照搬素材原文；禁止把素材里任何"
                        "面向守秘人的指导性文字（比如「可以让玩家自行设定……」「这样可以"
                        "活跃气氛」这类）读出来；不要发起检定。"
                    ),
                )
                narration = await self._narrate_prose(
                    situation,
                    opening_decision,
                    [],
                    [],
                    max_tokens=token_limit,
                    max_chars=char_limit,
                )
                narration = self._finalize_prose(
                    narration,
                    action_intent=False,
                    confused=False,
                    max_chars=char_limit,
                    room_id=room_id,
                )
                logger.info(
                    "keeper_opening_narrated",
                    room_id=room_id,
                    material_len=len(opening_material),
                    narration_len=len(narration),
                )
                return NarrationOutcome(text=narration)

        # 阶段1·裁决：结构化输出，检定是 schema 字段，不存在"忘了裁决"。
        decision = await self._adjudicate(situation)
        # 🔴 本轮撤销哪些能力（exec/27 阶段 3 · B 族）。此前这四处写成
        # `model_copy(update={"checks": [], ...})`——看着像清字段，实际是编排层在
        # 决定"这一轮禁止哪些能力生效"。写死字段名会把 agent.py 焊在具体能力上，
        # 而且加一片能力时没有任何地方提醒你"迷茫轮该不该收走它"。
        # 收口成能力集之后，这里只出现 Capability，不出现任何能力的字段名。
        revoked: set[Capability] = set()
        # 主动轮 / 开场仪式硬约束：不发起检定（设计：开场不发起高风险检定）
        if is_heartbeat or is_opening_ceremony:
            revoked |= CHECK_CAPABILITIES

        # 分类与代码强制（exec/27 阶段 4 抽进 `turn_policy`）。
        # 🔴 那条 if/elif 的**顺序就是语义**，且拆错会静默改行为——所以它有一份
        # 逐格钉死的表征测试（`test_turn_classification_characterization.py`），
        # 抽出去前后 27 格必须逐条不变。
        classification = classify_turn(
            decision,
            context.utterance,
            fallback_guidance=FALLBACK_ADJUDICATE_GUIDANCE,
            is_heartbeat=is_heartbeat,
            is_opening_ceremony=is_opening_ceremony,
        )
        decision = apply_code_forcing(
            decision,
            classification,
            utterance=context.utterance,
            spotlight_nickname=context.spotlight_nickname,
            is_heartbeat=is_heartbeat,
            is_opening_ceremony=is_opening_ceremony,
            revoked=frozenset(revoked),
        )

        char_limit = narration_limit(
            is_heartbeat=is_heartbeat,
            is_opening_ceremony=is_opening_ceremony,
            phase=phase,
            ending_reached=bool(decision.ending_reached),
        )
        token_limit = narration_max_tokens(char_limit)

        # 阶段2·执行：HP/状态纯代码立即写库；检定不在这里掷骰，解析成待掷记录。
        deps = KeeperDeps(
            room_id=room_id,
            player_id=context.player_id,
            session_factory=self._session_factory,
            module=self._module,
            ruleset=self._ruleset,
            reserved_state_keys=reserved_state_keys(),
            turn_player_ids=turn_player_ids,
            rng=self._rng,
        )
        # 守秘人的身份显式传进去：它不是"唯一那条代码路径"，是一个视图取
        # 全集、持全权限的主体（exec/14 P2）。全权限下 sanitize/authorize 都
        # 是恒等操作，行为与此前逐字节一致。
        # 裁决留痕（exec/25 #61）：写在所有代码强制改写**之后**、执行之前——
        # 这时 decision 是最终形态，而 player_state / thinking 仍是模型的原始
        # 输出（model_copy 只 update 指定字段），一条事件就能回答"叙事为什么
        # 这么写"。
        await record_decision(
            self._session_factory,
            room_id=room_id,
            player_id=context.player_id,
            decision=decision,
            forced=classification.forced_labels(spotlight=bool(context.spotlight_nickname)),
        )

        report, issues = await execute_side_effects(deps, decision, subject=KEEPER)
        pending_checks, pending_issues = await create_pending_checks(deps, decision, subject=KEEPER)
        issues = [*issues, *pending_issues]

        # 场景切换：独立于上面迷茫/怪话/明确行动三选一，两者可叠加生效。
        # 真人实测 2026-07-29：玩家还在跟邻居对话，宣告去书房，回复直接是
        # "钥匙已经转了半圈、门已经推开"，跳过了道别+赶路，读起来像瞬移。
        # 心跳/开场仪式各自已有独立的内容约束，跳过这条。
        #
        # 判据见 `location_state.scene_changed`（逐人位置比对，读执行后的状态）。
        after_state = await self._read_keeper_state(room_id)
        scene_changed = has_scene_changed(keeper_state, after_state, turn_player_ids)
        # 分段摘要 L2（exec/14 P4.2）：场景切换 = 天然的章节边界。**后台**整理，
        # 玩家等的是叙事，不该为"整理笔记"多等几秒；失败只记日志不影响这轮。
        if scene_changed and not is_heartbeat and not is_opening_ceremony:
            # 🔴 只喂**公开**历史行：L2 摘要本身不带受众，全房间共用一份，
            # 拿分头期间只有一边知道的剧情去压摘要，等于绕过 P5.2d 的裁剪从
            # 前情提要漏出去。代价是分头那段剧情进不了摘要（已在
            # `_narrate_per_audience` 的 docstring 里记为残留缺口）。
            self._spawn_chapter_summary(
                room_id, [line.text for line in history_lines if line.audience is None]
            )
            decision = decision.model_copy(
                update={
                    "narration_guidance": inject_scene_transition_guidance(
                        decision.narration_guidance
                    ),
                }
            )

        logger.info(
            "keeper_decision",
            thinking=decision.thinking,
            # 已经垂直切出去的能力自带审计字段（exec/27 阶段 2）——否则每加一片
            # 能力都得回来改这行，而漏了不报错、只是那片能力在日志里隐身。
            **audit_fields(decision),
            is_heartbeat=is_heartbeat,
            is_opening_ceremony=is_opening_ceremony,
            player_confused=classification.confused,
            clear_action_intent=classification.clear_action,
            weird_or_meta=classification.weird,
            scene_transition=scene_changed,
        )

        # exec/19 #44：动手了，但本轮一个检定都没裁出来（裁决器没发，或护栏
        # 拦掉）。此时叙事绝不能替玩家写出攻击的成败。
        unresolved_conflict = classification.physical_conflict and not pending_checks
        if unresolved_conflict:
            logger.info("keeper_unresolved_conflict", room_id=room_id)

        if pending_checks:
            async with self._session_factory() as db:
                await pending_check_manager.add(db, room_id, pending_checks)
                await db.commit()
            # 🔴 真人实测 2026-07-29：检定发起前的铺垫文字，不止会提前泄露
            # 检定结果（"东北角矮墓碑"这类招供内容），还会提前把检定对应的
            # 动作本身写成已经在成功进行（追踪检定前先写"沿着小径走了十几
            # 步"、潜行检定前先写"脚步声被夜风吞掉""三十步后你看见了"）——
            # 这是同一类问题的两个维度，旧版指引只堵了"信息"这一维。
            # 这段硬提醒改放在 user_content 最末尾（仿 length_hint 的位置，
            # 近因效应下模型服从概率更高），不再折进 narration_guidance
            # 中段——没法用代码保证模型一定服从（"这段话有没有替检定预支
            # 结果"不是能靠代码判断的），只能尽量提高服从概率。
            check_boundary_hint = build_check_boundary_hint(pending_checks)
            narration, segments = await self._narrate_per_audience(
                room_id=room_id,
                situation=situation,
                situation_builder=situation_builder,
                utterances=context.utterances,
                fallback_nickname=context.player_nickname,
                fallback_utterance=context.utterance,
                decision=decision,
                report=report,
                issues=issues,
                token_limit=token_limit,
                char_limit=char_limit,
                extra_suffix=check_boundary_hint,
                action_intent=classification.clear_action,
                confused=classification.confused,
                keeper_state=after_state,
                players=players,
                turn_player_ids=turn_player_ids,
                private_player_ids=frozenset(context.private_player_ids),
                on_delta=context.on_delta,
            )
            # 可观测性，不做自动拦截：能不能判断"这段话有没有替检定预支结果"
            # 没有可靠的代码手段（跟其它 scrub 规则不同，这是语义/因果判断，
            # 不是固定格式），删改有误伤合法对话铺垫的风险。这里只记录信号，
            # 供以后用批量质检工具（kp-quality-stress.py）专门跑一批"检定
            # 发起"场景、统计真实发生率，而不是只能靠真人测试偶然撞见。
            logger.info(
                "keeper_check_pending_narration",
                room_id=room_id,
                checks=[c.skill or "san" for c in pending_checks],
                narration_len=len(narration),
                narration_has_quoted_dialogue=("「" in narration or "”" in narration),
            )
            return NarrationOutcome(
                text=narration,
                check_requests=[to_notice(c) for c in pending_checks],
                stat_changes=deps.stat_changes,
                segments=segments,
            )

        # 阶段3·叙事：只写故事 + 长度硬裁 + 去菜单/软挡。
        # 动手却没裁出检定 → 换成"停在动作发生前并追问"，不许替玩家定成败
        # （exec/19 #44）。两条硬提醒方向相反，只能二选一。
        no_check_suffix = UNRESOLVED_CONFLICT_HINT if unresolved_conflict else NO_PENDING_CHECK_HINT
        narration, segments = await self._narrate_per_audience(
            room_id=room_id,
            situation=situation,
            situation_builder=situation_builder,
            utterances=context.utterances,
            fallback_nickname=context.player_nickname,
            fallback_utterance=context.utterance,
            decision=decision,
            report=report,
            issues=issues,
            token_limit=token_limit,
            char_limit=char_limit,
            extra_suffix=no_check_suffix,
            action_intent=classification.clear_action,
            confused=classification.confused,
            keeper_state=after_state,
            players=players,
            turn_player_ids=turn_player_ids,
            private_player_ids=frozenset(context.private_player_ids),
            on_delta=context.on_delta,
        )

        # HP 变化的可见性不再靠拼进叙事正文保证——那样等于让守秘人的嘴说了句
        # 不该它说的系统台词（真人实测 2026-07-28 反馈）。现在 deps.stat_changes
        # 走 character.stat_changed 结构化广播，前端渲染成独立的系统提示，
        # 和叙事气泡分开。
        return NarrationOutcome(text=narration, stat_changes=deps.stat_changes, segments=segments)

    async def resolve_check(
        self,
        room_id: str,
        player_id: str,
        check_request_id: str,
        on_result: CheckResultCallback | None = None,
    ) -> NarrationOutcome:
        """结算一次玩家确认的掷骰（两段式玩家掷骰）。

        队列还没清空：只广播这次的结果，不叙事——等玩家把本轮所有待掷检定
        都掷完。队列清空：复用 `narrate()` 触发一轮"结算叙事"——裁决器能
        在历史（keeper.check/keeper.san 事件）里看到刚掷出的结果，据此裁决
        后续（可能链式追加新的检定，比如目击后的理智检定，自然进入下一轮
        pending）。
        """
        # 🔴 pop 与"掷错人时放回队首"必须在**同一个事务**里：中间那次判断如果
        # 跨了事务，pop 已经提交而 requeue 失败就会把一次待掷检定凭空吃掉。
        async with self._session_factory() as db:
            pending = await pending_check_manager.pop(db, room_id, check_request_id)
            if pending is None:
                raise KeeperToolError("没有这个待掷的检定（可能已被结算）")
            if pending.player_id != player_id:
                await db.rollback()
                raise KeeperToolError(f"这个检定应由 {pending.player_nickname} 来掷")
            await db.commit()

        deps = KeeperDeps(
            room_id=room_id,
            player_id=pending.player_id,
            session_factory=self._session_factory,
            module=self._module,
            ruleset=self._ruleset,
            reserved_state_keys=reserved_state_keys(),
            rng=self._rng,
        )
        # 🔴 结算走注册表（exec/27 阶段 4·第八个钩子）：哪一片能力认领哪一种
        # 待掷记录由它自己声明。此前这里是一条按 kind 写死的 if/else——而"发起"
        # 那一半早就注册表化了，**同一件事的两头一头可插拔一头写死**。
        # `settler_for` 找不到认领者时直接抛，没有 else 兜底：兜底就是静默走错
        # 分支，掷骰数字照样出现在玩家屏幕上而没有任何东西会红。
        notice = await settler_for(pending.kind)(deps, pending)

        # 事实账本 L1（exec/14 P4）：检定成功 → 把这次揭开的线索**用代码**记进
        # 账本。不靠 LLM 自觉写 keeper_state，也不放进会滑出 200 条窗口的历史里
        # ——"必须记住"的东西必须活过窗口，这正是账本存在的理由。
        if pending.reveals and is_success(notice.level):
            # 受众 = 当时**跟他在同一处**的人（P5.2d）。分头时地下室挣到的线索
            # 不该出现在门厅那段的上下文里；未分头 → None → 照旧全房间可见。
            audience = await self._colocated_players(room_id, pending.player_id)
            async with self._session_factory() as db:
                await record_revelations(
                    db,
                    room_id=room_id,
                    player_id=pending.player_id,
                    fact_ids=list(pending.reveals),
                    via="check",
                    detail=f"{pending.skill or ''}·{notice.level}",
                    audience=audience,
                )
                await db.commit()

        logger.info(
            "keeper_check_resolved",
            room_id=room_id,
            check_request_id=check_request_id,
            kind=pending.kind,
            player=pending.player_nickname,
        )

        # 🔴 骰子已经落地了——立刻告诉调用方，不要等下面那次结算叙事。
        # 掷骰是纯代码毫秒级，结算叙事是 10 秒级的 LLM 往返；两件事一起等完
        # 再广播，玩家点完「投掷」得盯着屏幕十几秒才看得到自己掷了多少
        # （真人实测反馈：「反馈太慢」）。真人桌上骰子是**当场**停下的，
        # KP 想怎么描述是他自己的事。
        if on_result is not None:
            await on_result(notice)

        async with self._session_factory() as db:
            next_pending = await pending_check_manager.first(db, room_id)
        if next_pending is not None:
            return NarrationOutcome(
                text="",
                check_results=[notice],
                check_requests=[to_notice(next_pending)],
            )

        # 队列清空：结算叙事——复用 narrate()，让裁决器看到刚才的结果并续写。
        context = NarrationContext(
            utterance="（掷骰完成，请根据检定结果继续）",
            player_nickname=pending.player_nickname,
            room_id=room_id,
            player_id=player_id,
        )
        outcome = await self.narrate(context)
        return replace(outcome, check_results=[notice, *outcome.check_results])

    async def _adjudicate(self, situation: str) -> KeeperDecision:
        """委托给 `llm_calls.adjudicate`。

        🔴 **这层薄壳不是多余的**：整套测试都靠替换 `agent._adjudicate` /
        `agent._narrate_prose` 注入假模型输出（不打网络）。直接调模块函数就没有
        这个接缝了——抽 `llm_calls` 时我一度想删掉它们，会当场废掉十几个用例。
        """
        return await adjudicate(self._client, self._adjudicator_instructions, situation)

    async def _narrate_prose(
        self,
        situation: str,
        decision: KeeperDecision,
        report: list[str],
        issues: list[str],
        *,
        max_tokens: int,
        max_chars: int,
        extra_suffix: str = "",
    ) -> str:
        """委托给 `llm_calls.narrate_prose`（接缝理由同上）。"""
        return await narrate_prose(
            self._client,
            self._narrator_instructions,
            situation,
            decision,
            report,
            issues,
            max_tokens=max_tokens,
            max_chars=max_chars,
            extra_suffix=extra_suffix,
        )

    async def _narrate_prose_streamed(
        self,
        situation: str,
        decision: KeeperDecision,
        report: list[str],
        issues: list[str],
        *,
        token_limit: int,
        char_limit: int,
        extra_suffix: str,
        action_intent: bool,
        confused: bool,
        room_id: str | None,
        on_delta: NarrationDeltaSink,
    ) -> str:
        """流式叙事：边写边推，返回玩家实际收到的全文（`exec/28`）。

        🔴 返回值仍然是**完整的一段话**，调用方后面的落库、记账、历史都不用
        改——delta 只是提前把同样的内容送到了玩家眼前，不是新的事实来源。

        🔴 这里**不再调 `_finalize_prose`**：那三步（scrub / 泄密守门 / 长度）
        已经由 `NarrationStream` 按段施加过了，再跑一遍等于对已经推出去的文本
        做第二次裁剪，而推出去的字收不回来——两边结果一旦不同，玩家看到的和
        落库的就对不上。
        """
        stream = NarrationStream(
            narrate_prose_stream(
                self._client,
                self._narrator_instructions,
                situation,
                decision,
                report,
                issues,
                max_tokens=token_limit,
                max_chars=char_limit,
                extra_suffix=extra_suffix,
            ),
            module=self._module,
            action_intent=action_intent,
            confused=confused,
            max_chars=char_limit,
            room_id=room_id,
        )
        seq = 0
        async for piece in stream:
            await on_delta(seq, piece)
            seq += 1
        logger.info(
            "keeper_narration_streamed",
            room_id=room_id,
            segments=seq,
            raw_len=len(stream.raw),
            emitted_len=len(stream.text),
            truncated=stream.truncated,
        )
        return stream.text

    async def _narrate_per_audience(
        self,
        *,
        room_id: str,
        situation: str,
        situation_builder: SituationBuilder,
        utterances: tuple[PlayerUtterance, ...],
        fallback_nickname: str,
        fallback_utterance: str,
        decision: KeeperDecision,
        report: list[str],
        issues: list[str],
        token_limit: int,
        char_limit: int,
        extra_suffix: str,
        action_intent: bool,
        confused: bool,
        keeper_state: dict | None,
        players: list[tuple[str, str]],
        turn_player_ids: tuple[str, ...],
        private_player_ids: frozenset[str],
        on_delta: NarrationDeltaSink | None = None,
    ) -> tuple[str, list[NarrationSegment]]:
        """叙事阶段的投递分组（exec/14 P5.2，定稿：**一次裁决全局、只叙事分开**）。

        返回 `(全房间正文, 分组段落)`，两者互斥：
        - **全队同处一地、且本轮没有隐秘发言者** → 走原路径，返回 `(正文, [])`，
          prompt 与 P5.2 之前**逐字一致**（不追加任何范围提示）。这是退化保证。
        - 否则 → 返回 `("", 段落列表)`：
          · 每个**隐秘发言者**（②潜行中 或 ⑥自己标了私密）各一段，只给他本人；
          · 每个含**公开发言者**的位置组各一段，受众是该地点的**全部**在场调查员
            （没发言但人在现场的也该听见，隐匿者也在其中——「自己听得见」）；
          · 没人发言的地方本轮不生成叙事，那边没发生事，凭空写等于让模型编。
        - 延迟代价：1 次裁决 + N 段叙事，不是 N 次裁决。

        ## 🔴 保密靠的是"拿不到"，不是"请你别说"（P5.2d）

        每一段叙事的局面块都由 `situation_builder` **按这一段的受众重建**：
        历史、线索账本、本轮原话三处一起裁。门厅那段的模型上下文里根本没有
        地下室发生过什么，于是它想漏也漏不出来——这是结构性的，不是纪律性的。

        段尾那句范围提示保留，但它现在只承担文风（"只写这里的事"），不再是
        保密手段。

        ⚠️ 残留缺口，如实记：**L2 分段摘要**（前情提要）是全局历史压出来的，
        本身没有受众。现在的缓解是只用公开历史行去生成它（见
        `_spawn_chapter_summary` 的调用点），代价是分头期间的剧情不进摘要。
        """
        all_ids = [pid for pid, _ in players]
        groups = group_players(keeper_state, all_ids)
        # ②潜行是**常驻状态**（写在 keeper_state 里，直到被发现/现身）；
        # ⑥私密是**这一轮的一次性标记**（玩家自己在提交时勾的）。两者对投递的
        # 影响一样，但只有前者该在别人那段里被提"他藏着"。
        hidden_ids = load_hidden_players(keeper_state)
        covert_player_ids = hidden_ids | private_player_ids
        covert_speakers = [pid for pid in turn_player_ids if pid in covert_player_ids]
        open_speakers = {pid for pid in turn_player_ids if pid not in covert_player_ids}
        nicknames = dict(players)

        def _bystanders(audience: tuple[str, ...]) -> str:
            """这一段的受众里，本轮没发言的人（exec/19 #41）。按受众裁。"""
            return build_bystander_hint(
                [
                    nicknames[pid]
                    for pid in audience
                    if pid not in turn_player_ids and pid in nicknames
                ]
            )

        if len(groups) <= 1 and not covert_speakers:
            suffix = extra_suffix + _bystanders(tuple(all_ids))
            # 🔴 流式只走这条**全房间**路径（`exec/28` 第 3 步）。分头那条暂时
            # 保持非流式：它的延迟大头是**多段串行生成**（第 N 组要等前面 N-1 段
            # 全部写完），流式压不掉那部分——见 exec/28 的 3.4。
            if on_delta is not None:
                return (
                    await self._narrate_prose_streamed(
                        situation,
                        decision,
                        report,
                        issues,
                        token_limit=token_limit,
                        char_limit=char_limit,
                        extra_suffix=suffix,
                        action_intent=action_intent,
                        confused=confused,
                        room_id=room_id,
                        on_delta=on_delta,
                    ),
                    [],
                )
            narration = await self._narrate_prose(
                situation,
                decision,
                report,
                issues,
                max_tokens=token_limit,
                max_chars=char_limit,
                extra_suffix=suffix,
            )
            return (
                self._finalize_prose(
                    narration,
                    action_intent=action_intent,
                    confused=confused,
                    max_chars=char_limit,
                    room_id=room_id,
                ),
                [],
            )

        by_speaker = {u.player_id: u for u in utterances}

        def _said_by(members: tuple[str, ...]) -> tuple[str, str]:
            """这一组人本轮说了什么。别组的原话一个字都不带进来。"""
            said = [by_speaker[pid] for pid in members if pid in by_speaker]
            if not said:
                return fallback_nickname, fallback_utterance
            if len(said) == 1:
                return said[0].nickname, said[0].text
            # 与 service/turn_window.merge_utterances 同口径
            return said[0].nickname, "\n".join(f"{u.nickname}：{u.text}" for u in said)

        async def _segment(
            audience: tuple[str, ...], node_id: str | None, hint: str, *, covert: bool = False
        ):
            async with self._session_factory() as db:
                known = await visible_fact_ids(db, room_id=room_id, audience=frozenset(audience))
            nickname, said = _said_by(audience)
            scoped_situation = situation_builder.render(
                audience=frozenset(audience),
                ledger=render_ledger(self._module, known),
                nickname=nickname,
                utterance=said,
            )
            raw = await self._narrate_prose(
                scoped_situation,
                decision,
                report,
                issues,
                max_tokens=token_limit,
                max_chars=char_limit,
                extra_suffix=extra_suffix + hint + _bystanders(audience),
            )
            return NarrationSegment(
                text=self._finalize_prose(
                    raw,
                    action_intent=action_intent,
                    confused=confused,
                    max_chars=char_limit,
                    room_id=room_id,
                ),
                audience=audience,
                node_id=node_id,
                covert=covert,
            )

        segments: list[NarrationSegment] = []
        for pid in covert_speakers:
            who = nicknames.get(pid, pid)
            segments.append(
                await _segment(
                    (pid,),
                    location_of(keeper_state, pid),
                    (
                        f"\n\n【投递范围·代码硬提醒】这一段**只会送达 {who} 一个人**。"
                        f"他这一轮的行动是隐秘的：同一处的其他调查员不知道他做了什么。"
                        "因此：只写他自己感知到的结果；不要写别人对他这次行动的反应，"
                        "也不要写成好像大家都看见了。"
                    ),
                    covert=True,
                )
            )
        for node_id, members in groups:
            if not open_speakers.intersection(members):
                continue
            node = self._module.node_by_id(node_id) if node_id else None
            where = node.title if node is not None else (node_id or "此处")
            who = "、".join(nicknames.get(pid, pid) for pid in members)
            hint = (
                f"\n\n【投递范围·代码硬提醒】这一段**只会送达在「{where}」的 {who}**，"
                "别处的调查员看不到。因此：只写这里发生的事；"
                "其他调查员在别处的行动、发现、遭遇，一个字都不要提，"
                "也不要替这边的人转述他们不可能知道的消息。"
            )
            hidden_here = [nicknames.get(pid, pid) for pid in members if pid in hidden_ids]
            if hidden_here:
                hint += (
                    f"另外，{'、'.join(hidden_here)}正处于隐匿状态——这里的其他人"
                    "不知道他在场，正文里不要提到他。"
                )
            segments.append(await _segment(tuple(members), node_id, hint))
        logger.info(
            "keeper_narration_split",
            room_id=room_id,
            groups=[(nid, len(m)) for nid, m in groups],
            covert_speakers=len(covert_speakers),
            segments=len(segments),
        )
        return "", segments

    def _spawn_chapter_summary(self, room_id: str, history_lines: list[str]) -> None:
        """把摘要生成丢到后台。刻意不 await——它不在玩家等待路径上。"""
        task = asyncio.create_task(self._summarize_chapter(room_id, history_lines))
        # 存一份引用防止任务被 GC 提前回收（asyncio 只持弱引用）
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _summarize_chapter(self, room_id: str, history_lines: list[str]) -> None:
        """整理一段梗概。任何失败都只记日志——它是记忆的锦上添花，不是主路径。"""
        try:
            async with self._session_factory() as db:
                turns = await turns_since_last_chapter(db, room_id=room_id)
            if not should_summarize(scene_changed=True, turns_since_last=turns):
                return
            if not history_lines:
                return
            text = await summarize_chapter(self._client, history_lines)
            async with self._session_factory() as db:
                await record_chapter(db, room_id=room_id, text=text)
                await db.commit()
        except Exception:
            logger.warning("keeper_chapter_summary_failed", room_id=room_id, exc_info=True)

    def _finalize_prose(
        self,
        text: str,
        *,
        action_intent: bool,
        confused: bool = False,
        max_chars: int,
        room_id: str | None = None,
    ) -> str:
        scrubbed = scrub_kp_anti_patterns(text, action_intent=action_intent, confused=confused)
        # 泄密守门（exec/14 P3）：元层断言被逐字复述 → 整句丢弃；片段命中只
        # 记日志不删（元层里常含公开人名/地名，片段匹配必然误伤合法叙事）。
        # 这是全部三条叙事路径的唯一咽喉，放这里就不会有旁路。
        scrubbed, leak_hits = scrub_meta_leaks(scrubbed, self._module)
        log_leak_hits(leak_hits, room_id=room_id)
        # scrub 后再 clip，避免删菜单后仍超长 / 或裁切前未处理的尾巴
        final = clip_narration(scrubbed, max_chars)
        if scrubbed != (text or "").strip() or final != scrubbed:
            logger.info(
                "keeper_narration_scrubbed",
                before=len(text or ""),
                after=len(final),
                action_intent=action_intent,
                confused=confused,
            )
        return final

    async def _colocated_players(self, room_id: str, player_id: str) -> list[str] | None:
        """跟他同处一地的玩家 id；**未分头时返回 None = 全房间**（P5.2d）。

        找不到他时返回 `[player_id]`，与 ws 层同一条规矩：这条路径上的错误
        必须朝保密方向失败。
        """
        async with self._session_factory() as db:
            room = await db.get(Room, room_id)
            keeper_state = room.keeper_state if room is not None else None
            # AI 玩家算进分组（exec/21 第一层）：它在场就该算进它那一组，
            # 否则分头时它拿不到自己那边的叙事。**分组用全量玩家、发送用连接**
            # ——`send_to_players` 按 player_id 找连接，AI 没有连接自然发不到，
            # 那是对的，不需要在这里特判。
            rows = await db.execute(select(Player.id).where(Player.room_id == room_id))
            ids = list(rows.scalars())
        groups = group_players(keeper_state, ids)
        if len(groups) <= 1:
            return None
        for _node_id, members in groups:
            if player_id in members:
                return members
        return [player_id]

    async def _read_keeper_state(self, room_id: str) -> dict | None:
        """只读一次世界状态笔记。执行阶段写库之后要拿新值时用（P5.2 场景变化判定）。"""
        async with self._session_factory() as db:
            room = await db.get(Room, room_id)
            return room.keeper_state if room is not None else None

    async def _load_room_memory(
        self, room_id: str
    ) -> tuple[dict | None, list[HistoryLine], list[str], list[tuple[str, str]]]:
        """读取世界状态笔记 + 全量事件历史 + 在场调查员名单。

        与 build_narration_context 的 6 条窗口不同：守秘人要对整局的一致性
        负责，所以这里重放的是**最近 `HISTORY_LIMIT`（200）条**事件，比叙事
        那 6 条宽得多。

        ⚠️ 这是**滑动窗口，不是完整历史**（此处 docstring 曾写"重放完整历史"，
        与实现矛盾，exec/14 P4 修正）。每轮产生 2–4 条事件，200 条约等于最近
        50–80 轮；一场几十小时的战役会把开头静默挤出去。**必须记住的东西不
        依赖这个窗口**——线索走 `fact_ledger`（代码记账、读全量），世界状态走
        `keeper_state`，两者都活过窗口。

        名单必须显式注入：真实 DeepSeek 冒烟里，agent 不知道桌上有几个人，
        开场直接幻觉出"你们三人"（实际只有一名玩家）——在场有谁不该靠猜。
        """
        async with self._session_factory() as db:
            room = await db.get(Room, room_id)
            keeper_state = room.keeper_state if room is not None else None

            player_rows = list(
                (await db.execute(select(Player).where(Player.room_id == room_id))).scalars()
            )
            character_rows = list(
                (await db.execute(select(Character).where(Character.room_id == room_id))).scalars()
            )
            chars_by_player = {c.player_id: c for c in character_rows}
            # 🔴 名单里带上角色卡摘要（exec/23 #55）：此前守秘人对玩家的全部
            # 认知就是"名字 + 职业"两项，玩家问「我是谁」时它只能现编个人史。
            # 真人 KP 面前摊着每个人的卡——这里把卡摆到桌面上。
            roster = [
                format_sheet(p.nickname, chars_by_player.get(p.id), self._ruleset)
                for p in player_rows
            ]
            # (player_id, 昵称)：位置分组要按 id 分，渲染给 LLM 要用昵称。
            # 🔴 AI 玩家进名单（exec/21 第一层）：守秘人必须知道桌上有几个人
            # ——这份名单当初就是为了治"单人局幻觉成你们三人"。AI 在场却不在
            # 名单里，叙事会当它不存在。
            players = [(p.id, p.nickname) for p in player_rows]

            result = await db.execute(
                select(Event)
                .where(
                    Event.room_id == room_id,
                    Event.event_type.in_(HISTORY_EVENT_TYPES),
                )
                .order_by(Event.created_at.desc(), Event.id.desc())
                .limit(HISTORY_LIMIT)
            )
            events = list(result.scalars())
            events.reverse()

            # 历史行的昵称直接用上面已查出的成员表（老成员退出房间的场景本期
            # 不存在，player_rows 就是全量）。
            nicknames = {p.id: p.nickname for p in player_rows}

        # 🔴 渲染逻辑在 history.py，与 AI 玩家共用同一份（exec/21 第三层）——
        # 两份读法迟早会不一致，而不一致的方向一定是"AI 看到了不该看的"。
        lines = history_lines_from_events(events, nicknames)
        return keeper_state, lines, roster, players
