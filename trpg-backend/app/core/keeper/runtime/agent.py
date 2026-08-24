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
import time
from collections.abc import Awaitable
from dataclasses import replace
from uuid import uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.keeper.access.leak_guard import log_leak_hits, scrub_meta_leaks
from app.core.keeper.access.subject import KEEPER
from app.core.keeper.capabilities import (
    audit_fields,
    post_settle_for,
    post_settles,
    reserved_state_keys,
    settle_hook_for,
)
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.contract.registry import Capability
from app.core.keeper.memory.chapter import (
    chapter_budget,
    events_since_last_chapter,
    record_chapter,
    should_summarize,
    split_history_for_chapters,
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
from app.core.keeper.memory.recall import format_recall, recall_history
from app.core.keeper.narration.narration_hints import (
    NO_PENDING_CHECK_HINT,
    UNRESOLVED_CONFLICT_HINT,
    build_bystander_hint,
    build_check_boundary_hint,
    build_clarification_guidance,
    build_opening_cast_hint,
    build_person_hint,
)
from app.core.keeper.narration.prompts import (
    build_adjudicator_instructions,
    build_narrator_instructions,
)
from app.core.keeper.narration.prose_discipline import (
    clip_narration,
    inject_closure_guidance,
    inject_finale_guidance,
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
from app.core.keeper.runtime.end_game import propose_end_game
from app.core.keeper.runtime.llm_calls import (
    FALLBACK_ADJUDICATE_GUIDANCE,
    adjudicate,
    narrate_prose,
    narrate_prose_stream,
    request_timeout_seconds,
    summarize_chapter,
)
from app.core.keeper.runtime.location_state import (
    group_players,
    load_hidden_players,
    location_of,
    resolve_location,
)
from app.core.keeper.runtime.location_state import (
    scene_changed as has_scene_changed,
)
from app.core.keeper.runtime.narration_stream import NarrationStream
from app.core.keeper.runtime.pending import (
    LUCK_SPEND_KIND,
    MERGE_CONFIRM_KIND,
    ROLL_KINDS,
    TURN_BLOCKING_KINDS,
    PendingDecision,
    pending_decision_manager,
    to_notice,
)
from app.core.keeper.runtime.phase import (
    PHASE_ENDING,
    PHASE_FINISHED,
    PHASE_INVESTIGATION,
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
    CheckResultNotice,
    NarrationContext,
    NarrationDeltaSink,
    NarrationOutcome,
    NarrationSegment,
    Narrator,
    PlayerUtterance,
    SegmentDeltaSinkFactory,
)
from app.core.narration.deepseek import deepseek_base_url
from app.dto.game import RulesetRead
from app.models.event import Event
from app.models.room import Character, Player, Room

logger = structlog.get_logger()

#: 待决定守卫命中时守秘人说的话（`exec/23 #76`）。
#:
#: 🔴 **这一刻正是零基础玩家第一次看见「检定」「幸运」这些词的时刻**，而原文
#: 是一句机器提示（「守秘人正在等待掷骰——请先完成待掷的检定」）。真机一局里
#: 同一根因复现两次：问「这卡片什么意思／掷不好是不是就什么都找不到了」
#: 「幸运是啥、花掉还能长回来吗」，都被这句话顶回来。
#:
#: 修法是**让守卫自己把这张卡讲清楚**，不调模型：分类"这句话是不是在问规则"
#: 得跑一次模型往返，而守卫存在的理由之一正是"别在等骰子的时候再开一轮"。
#: 代价是它只答得了这张卡的事，答不了别的——那正好是新手在这一刻会问的。
#:
#: 🔴 **不许带技能名、理由、点数**：那些字段本来就在卡片上，而卡片是按受众
#: 发的——写进这里就是把刚修好的 `exec/33 #78` 又开一个口子（"加兜底前先问
#: 它会不会跟已有规则组成闭环"）。
#:
#: 🔴 **这两句是第二人称祈使句，所以只能发给卡的主人**（2026-08-11 双人真机）：
#: 原先它们走全房间广播，于是手上没有卡的玩家也被要求「先把手上那张检定卡
#: 掷了」，而他点不出任何东西。被顶回来的其他发言者改收 `PENDING_WAIT_NOTICE`
#: ——**不能什么都不回**（「按钮没有缓冲区」的同族：说了话没有任何回应，玩家
#: 只会认为坏了）。
ROLL_PENDING_NOTICE = (
    "先把手上那张检定卡掷了——点一下卡片，骰子由我来掷，"
    "掷出来是多少就是多少。掷不好也不会把线索卡死，只是这条路会更慢、"
    "或者要付点别的代价。掷完我们接着说。"
)
LUCK_PENDING_NOTICE = (
    "先回答手上那张卡：要不要花幸运把这次检定改成成功。"
    "幸运是你角色的一项属性，花掉就少掉那么多、不会自己长回来，"
    "所以留着还是现在用，你自己定。选完我们接着说。"
)


def build_pending_wait_notice(owner_nickname: str) -> str:
    """本轮被待决定守卫顶回来、但手上没有那张卡的人收到的话。

    只提昵称（房间成员列表本来就看得见），不提技能名/理由/点数——那些在卡片
    上，而卡片是按受众发的。
    """
    return f"稍等一下：{owner_nickname}手上还有一张卡要先处理，处理完我们接着说。"


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
            api_key=api_key, base_url=deepseek_base_url(), timeout=request_timeout_seconds()
        )
        self._background: set[asyncio.Task] = set()
        #: 分头轮次计数：磁带子键要靠它区分"哪一轮的第几段"。由代码递增，
        #: 跟模型输出无关，所以录制与回放会走到同一个值。
        self._split_turn_ordinal = 0
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
            pending = await pending_decision_manager.first(db, room_id, TURN_BLOCKING_KINDS)
            owner_nickname = ""
            if pending is not None:
                owner = (
                    await db.execute(select(Player).where(Player.id == pending.player_id))
                ).scalar_one_or_none()
                owner_nickname = owner.nickname if owner is not None else "另一位调查员"
        if pending is not None:
            if is_heartbeat or is_opening_ceremony:
                return NarrationOutcome(text="")
            logger.info(
                "keeper_narrate_pending_guard",
                room_id=room_id,
                check_request_id=pending.decision_id,
                kind=pending.kind,
            )
            # 🔴 重发的是**那一项本来的形状**：幸运卡不能当成检定请求重发，
            # 否则玩家收到一张点了会报「没有这个待掷的检定」的卡片
            # （同族于「加一种 kind 就要检查每个逐个列出类别的消费方」）。
            #: 受众分两段：卡的主人收祈使句，本轮**其他**被顶回来的发言者收一句
            #: 说明。不在本轮发言的人不发——他没被顶回来，多一条只是噪声。
            waiting = tuple(pid for pid in turn_player_ids if pid != pending.player_id)
            segments = [
                NarrationSegment(
                    text=(
                        LUCK_PENDING_NOTICE
                        if pending.kind == LUCK_SPEND_KIND
                        else ROLL_PENDING_NOTICE
                    ),
                    audience=(pending.player_id,),
                )
            ]
            if waiting:
                segments.append(
                    NarrationSegment(
                        text=build_pending_wait_notice(owner_nickname),
                        audience=waiting,
                    )
                )
            if pending.kind == LUCK_SPEND_KIND:
                return NarrationOutcome(text="", segments=segments, player_offers=[pending])
            return NarrationOutcome(
                text="",
                segments=segments,
                check_requests=[to_notice(pending)],
            )

        keeper_state, history_lines, roster, players = await self._load_room_memory(room_id)

        phase = load_phase(keeper_state)
        ending_id = load_ending_id(keeper_state)

        # 🔴 收尾是可撤回的（2026-08-12）：`ending` 阶段里**玩家继续说话本身
        # 就是"我们还想玩"**——不需要额外问一句，也不需要新交互。心跳与开场
        # 不算数（那不是玩家的意思表示），所以它们不触发退回，只是静默。
        #
        # 这一步存在的理由是让收尾**判早了的代价变小**：从"对局不可撤回地结束"
        # 降成"多写一段终章"。代价小了，那道替 KP 做判断的机械前提才拿得掉。
        reopened_from_ending = False
        if phase == PHASE_ENDING:
            if is_heartbeat or is_opening_ceremony:
                return NarrationOutcome(text="")
            reopened_from_ending = True
            deps_resume = KeeperDeps(
                room_id=room_id,
                player_id=context.player_id,
                session_factory=self._session_factory,
                module=self._module,
                ruleset=self._ruleset,
                reserved_state_keys=reserved_state_keys(),
                turn_player_ids=turn_player_ids,
            )
            await set_phase_impl(deps_resume, PHASE_INVESTIGATION)
            logger.info("keeper_closure_reopened", room_id=room_id, player_id=context.player_id)
            keeper_state, history_lines, roster, players = await self._load_room_memory(room_id)
            phase = load_phase(keeper_state)

        # 对局已结束：拒绝新行动（心跳亦静默）
        if phase == PHASE_FINISHED:
            if is_heartbeat or is_opening_ceremony:
                return NarrationOutcome(text="")
            # 🔴 **不要把 `ending_id` 印给玩家**（2026-08-16 真机）：那是内部
            # slug（实测印出来的是 `ending-adventure-options`）。`phase.py` 的
            # `format_phase_status` 印 id 是对的——那份是给**裁决器**看的局面块，
            # 它就该按 id 认结局。两处此前共用了一个格式，而受众完全不同。
            # 这个仓库为「label 泄漏内部 id」红过一次，同一个病换了个位置。
            #
            # 也不必在这里复述结局：终章由命中结局那一拍的叙事写完了
            # （`inject_finale_guidance`），这句只是**之后**任何行动的挡板。
            return NarrationOutcome(text="本局已结束。感谢各位调查员。")

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
            ruleset=self._ruleset,
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
                # 🔴 开场也要按在场者定人称与在场感（`exec/33 #84`）：这一拍走的是
                # 独立分支，`_narrate_per_audience` 里那两个提示够不着它。开场
                # **不分段**，受众就是全房间。
                opening_cast = [n for _, n in players]
                narration = await self._narrate_prose(
                    situation,
                    opening_decision,
                    [],
                    [],
                    max_tokens=token_limit,
                    max_chars=char_limit,
                    extra_suffix=(
                        build_person_hint(opening_cast) + build_opening_cast_hint(opening_cast)
                    ),
                )
                narration = self._finalize_prose(
                    narration,
                    action_intent=False,
                    confused=False,
                    max_chars=char_limit,
                    room_id=room_id,
                    vocatives=frozenset(n for _, n in players),
                )
                logger.info(
                    "keeper_opening_narrated",
                    room_id=room_id,
                    material_len=len(opening_material),
                    narration_len=len(narration),
                )
                return NarrationOutcome(text=narration)

        # 玩家纠错轮（`exec/35`）：把澄清摆到裁决器眼前。指针已经由调用方
        # 回滚过了，这里只负责让它按澄清后的意思重裁那一轮的原话。
        if context.clarification:
            situation = f"{situation}\n\n{build_clarification_guidance(context.clarification)}"

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

        # 玩家提议收工时这一拍要额外推出去的确认卡（走 `player_offers` 那条
        # 现成通道——它本来就是「等某个玩家答一句」的统称，不是幸运卡专用）。
        end_game_cards: list = []

        # 🔴 **玩家说"结束了吧"时，那句话不是"我们还想玩"**（2026-08-15）。
        #
        # 上面那条退回规则假设了"玩家继续说话 = 还想玩"。08-14 实测里它正好
        # 反过来：玩家已经回城复命完毕，连说三次「可以结束了」「结束了吧」，
        # 每一次都被判成还想玩，**对局就是结束不了**。那三句根本不是角色台词，
        # 是他抬起头跟主持人讲话——**出戏的话被当成戏内发言喂进了裁决**。
        #
        # 判"这句是不是出戏想收场"是语义判断，交给裁决 LLM（新增第八格
        # `wrap_up`，同 `confused` / `feasibility_question` 的先例）。代码只
        # 消费它。
        #
        # 🔴 **2026-08-19：它不再要求「收尾门先开过」。** 原来绑死在
        # `reopened_from_ending` 上，于是玩家手上只有否决权和确认权、**没有
        # 发起权**——而真人线下团里，收尾最高频的入口恰恰是玩家自己宣布的
        # （「我们报警，然后回家」）。收尾门问的是「内容跑完了没有」，这句话
        # 问的是「我们还想不想玩」，是两个独立的信号（同「卡住了和做完了是
        # 相反的处境」那条判据）。
        #
        # 挡住误判的东西**从「门」换成了「一次显式点击」**：这里只发提议，
        # 结束与否由 `end_game.py` 那张全桌确认卡决定，单人局也要点一次。
        #
        # 按 `exec/20` 的口径：这是概率性改进（触发条件由 LLM 判），不说"已修复"。
        if getattr(decision, "player_state", None) == "wrap_up":
            async with self._session_factory() as db:
                proposal = await propose_end_game(db, room_id, context.player_id)
                await db.commit()
            logger.info(
                "keeper_end_game_proposed",
                room_id=room_id,
                player_id=context.player_id,
                cards=len(proposal.cards),
                waiting_for=list(proposal.waiting_for),
            )
            end_game_cards = list(proposal.cards)

        if reopened_from_ending:
            # 收束纪律：不管这一拍收不收得成，**都该按收场写**（铺尾声、不抛
            # 新线索、给最后一次动作机会）。此前 `ending` 阶段除了放宽字数
            # 一条纪律都没有，玩家收到的还是一段普通调查叙事。
            decision = decision.model_copy(
                update={"narration_guidance": inject_closure_guidance(decision.narration_guidance)}
            )

        # 🔴 命中**剧本预设结局**的那一拍要写成终章（2026-08-16 真机）。
        # 上面那一支只覆盖开放式收尾——它要求先进过 `ending` 阶段再退回来，而
        # `ending_reached` 这条路 `progression` 当轮直接置 finished，**不经过
        # `ending`**，于是一条纪律都注入不到。同一件事两条路，此前只接通了一条。
        # 顺带把剧本写好的那段落幕点名喂进去：它此前只躺在系统 prompt 末尾的
        # 剧本全文里，跟没命中的那几条结局并排，等于没喂。
        hit_ending = next(
            (e for e in self._module.endings if e.id == getattr(decision, "ending_reached", None)),
            None,
        )
        if hit_ending is not None:
            decision = decision.model_copy(
                update={
                    "narration_guidance": inject_finale_guidance(
                        decision.narration_guidance, hit_ending.text
                    )
                }
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
        # 🔴 **摘要的调用点跟场景切换解绑**（2026-08-16）：它原来挂在下面那个
        # `if scene_changed` 里，于是 `should_summarize` 新加的兜底上限**永远
        # 走不到**——不换场景就一次都不摘，那段剧情滚出 L3 之后再也重建不了。
        # 这正是「两件事共用一个开关」那条判据（`keeper.phase` 已经栽过一次）：
        # 过渡拍的注入确实只该在换场景时做，而摘要有它自己的判据。
        #
        # 摘要**按受众分段**（2026-08-11 补上 P5.2d 的残留缺口）：公开的一段
        # + 分头那几组各自一段，落库时带受众，注入时按受众裁。
        # 此前是"只喂公开行"——安全但把分头期间的剧情整段丢掉，分头越久
        # 记忆里那段越空。
        # 🔴 传在场名单：受众覆盖全场的行**本来就是公开的**，不该再单独
        # 摘一段（实测单人局每次摘要都出两份，2× LLM 调用 + L2 里重复）。
        if not is_heartbeat and not is_opening_ceremony:
            self._spawn_chapter_summary(
                room_id,
                history_lines,
                frozenset(p for p, _ in players),
                scene_changed=scene_changed,
            )
        if scene_changed and not is_heartbeat and not is_opening_ceremony:
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
                await pending_decision_manager.add(db, room_id, pending_checks)
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
                segment_delta_sink=context.segment_delta_sink,
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
                player_offers=end_game_cards,
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
            segment_delta_sink=context.segment_delta_sink,
        )

        # HP 变化的可见性不再靠拼进叙事正文保证——那样等于让守秘人的嘴说了句
        # 不该它说的系统台词（真人实测 2026-07-28 反馈）。现在 deps.stat_changes
        # 走 character.stat_changed 结构化广播，前端渲染成独立的系统提示，
        # 和叙事气泡分开。
        return NarrationOutcome(
            text=narration,
            stat_changes=deps.stat_changes,
            segments=segments,
            player_offers=end_game_cards,
        )

    async def resolve_check(
        self,
        room_id: str,
        player_id: str,
        check_request_id: str,
        on_result: CheckResultCallback | None = None,
        roll_value: int | None = None,
    ) -> NarrationOutcome:
        """结算一次玩家确认的掷骰（两段式玩家掷骰）。

        `roll_value` 是玩家用桌上实体骰掷出来的出目（`exec/46` B5）。
        **None = 服务端掷**，那是默认行为。这一层只负责把它带下去，
        "这个房间准不准报数"由调用方（`ws.py`）判——它才有房间那行。

        队列还没清空：只广播这次的结果，不叙事——等玩家把本轮所有待掷检定
        都掷完。队列清空：复用 `narrate()` 触发一轮"结算叙事"——裁决器能
        在历史（keeper.check/keeper.san 事件）里看到刚掷出的结果，据此裁决
        后续（可能链式追加新的检定，比如目击后的理智检定，自然进入下一轮
        pending）。
        """
        # 🔴 pop 与"掷错人时放回队首"必须在**同一个事务**里：中间那次判断如果
        # 跨了事务，pop 已经提交而 requeue 失败就会把一次待掷检定凭空吃掉。
        async with self._session_factory() as db:
            pending = await pending_decision_manager.pop(db, room_id, check_request_id)
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
            manual_roll=roll_value,
        )
        # 🔴 结算走注册表（exec/27 阶段 4·第八个钩子）：哪一片能力认领哪一种
        # 待掷记录由它自己声明。此前这里是一条按 kind 写死的 if/else——而"发起"
        # 那一半早就注册表化了，**同一件事的两头一头可插拔一头写死**。
        # `settle_hook_for` 找不到认领者时直接抛，没有 else 兜底：兜底就是静默
        # 走错分支，掷骰数字照样出现在玩家屏幕上而没有任何东西会红。
        notice = await settle_hook_for(pending.kind).run(deps, pending)

        # 🔴 骰子已经落地了——立刻告诉调用方，不要等下面那些副作用和结算叙事。
        # 掷骰是纯代码毫秒级，结算叙事是 10 秒级的 LLM 往返；两件事一起等完
        # 再广播，玩家点完「投掷」得盯着屏幕十几秒才看得到自己掷了多少
        # （真人实测反馈：「反馈太慢」）。真人桌上骰子是**当场**停下的，
        # KP 想怎么描述是他自己的事。
        if on_result is not None:
            await on_result(notice)

        # 🔴 第九个钩子（exec/34 第 4 步）：结算之后还要不要再等玩家一拍。
        # 幸运消费就挂在这里——骰子已经停下、结果还没生效的那个窗口。
        # **生效必须留到决定之后**，否则花完幸运就得逐个回滚副作用。
        for hook in post_settles():
            offer = await hook.offer(deps, pending, notice)
            if offer is None:
                continue
            async with self._session_factory() as db:
                await pending_decision_manager.add(db, room_id, [offer])
                await db.commit()
            logger.info(
                "keeper_post_settle_offered",
                room_id=room_id,
                kind=offer.kind,
                player=offer.player_nickname,
            )
            return NarrationOutcome(text="", check_results=[notice], player_offers=[offer])

        return await self._after_check(room_id, player_id, deps, pending, notice)

    async def resolve_player_offer(
        self,
        room_id: str,
        player_id: str,
        decision_id: str,
        accepted: bool,
        on_result: CheckResultCallback | None = None,
    ) -> NarrationOutcome:
        """玩家答完了「结算之后那一拍」（现在只有幸运消费）。

        答完才继续走生效 → 事实账本 → 结算叙事，跟 `resolve_check` 共用同一条
        尾巴。**改写过的结果通知会再广播一次**：玩家花掉幸运之后，屏幕上那个
        「失败」必须当场变成「成功」。
        """
        async with self._session_factory() as db:
            offer = await pending_decision_manager.pop(db, room_id, decision_id)
            if offer is None:
                raise KeeperToolError("没有这个待决定项（可能已被处理）")
            if offer.player_id != player_id:
                await db.rollback()
                raise KeeperToolError(f"这个决定应由 {offer.player_nickname} 来做")
            await db.commit()

        deps = KeeperDeps(
            room_id=room_id,
            player_id=offer.player_id,
            session_factory=self._session_factory,
            module=self._module,
            ruleset=self._ruleset,
            reserved_state_keys=reserved_state_keys(),
            rng=self._rng,
        )
        pending, notice = await post_settle_for(offer.kind).resolve(deps, offer, accepted)
        if on_result is not None:
            await on_result(notice)
        return await self._after_check(room_id, player_id, deps, pending, notice)

    async def _after_check(
        self,
        room_id: str,
        player_id: str,
        deps: KeeperDeps,
        pending: PendingDecision,
        notice: CheckResultNotice,
    ) -> NarrationOutcome:
        """一次检定**生效之后**的公共尾巴：记账 → 事实账本 → 下一张卡或结算叙事。

        两个入口共用（直接结算 / 答完幸运那一拍），所以**改写过的 notice 会
        一路带下去**——事实账本按它判成败，结算叙事看到的也是它。
        """
        await settle_hook_for(pending.kind).apply(deps, pending, notice)

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
            check_request_id=notice.check_request_id,
            kind=pending.kind,
            player=pending.player_nickname,
        )

        async with self._session_factory() as db:
            next_pending = await pending_decision_manager.first(db, room_id, ROLL_KINDS)
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
        tape_key: str | None = None,
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
            tape_key=tape_key,
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
        tape_key: str | None = None,
        vocatives: frozenset[str] = frozenset(),
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
                tape_key=tape_key,
            ),
            module=self._module,
            action_intent=action_intent,
            confused=confused,
            vocatives=vocatives,
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
            # 这两个数配合 `keeper_adjudicate_timing` 才能回答"5 秒花在哪一拍"。
            # 单看任何一个都会得出错的结论——我就这么错过一次。
            first_delta_ms=round(stream.first_delta_ms) if stream.first_delta_ms else None,
            total_ms=round(stream.total_ms) if stream.total_ms else None,
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
        segment_delta_sink: SegmentDeltaSinkFactory | None = None,
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
        # 🔴 分组要知道谁在等确认会合（`exec/34` 定的"多查一次库"）：确认之前
        # 那个人自己一组，这是投递隔离的地基。真相在待决定队列里，不在
        # keeper_state——所以这里查一次，而不是留一份镜像。
        async with self._session_factory() as db:
            merge_pending = await pending_decision_manager.player_ids_of_kind(
                db, room_id, MERGE_CONFIRM_KIND
            )
        groups = group_players(keeper_state, all_ids, merge_pending)
        # ②潜行是**常驻状态**（写在 keeper_state 里，直到被发现/现身）；
        # ⑥私密是**这一轮的一次性标记**（玩家自己在提交时勾的）。两者对投递的
        # 影响一样，但只有前者该在别人那段里被提"他藏着"。
        hidden_ids = load_hidden_players(keeper_state)
        covert_player_ids = hidden_ids | private_player_ids
        covert_speakers = [pid for pid in turn_player_ids if pid in covert_player_ids]
        open_speakers = {pid for pid in turn_player_ids if pid not in covert_player_ids}
        nicknames = dict(players)
        #: 在场者昵称集合：纪律层砍掉机制播报后，靠它判断"剩下的还是不是一句话"
        #: （`exec/33 #82`：「阿福，该你掷侦察了。」砍完只剩「阿福。」）。
        #: 🔴 **全房间与分头两条路都要传**，漏一条就是那条路上仍然留残句。
        vocatives = frozenset(nicknames.values())

        def _bystanders(audience: tuple[str, ...]) -> str:
            """这一段的受众里，本轮没发言的人（exec/19 #41）。按受众裁。"""
            return build_bystander_hint(
                [
                    nicknames[pid]
                    for pid in audience
                    if pid not in turn_player_ids and pid in nicknames
                ]
            )

        def _person(audience: tuple[str, ...]) -> str:
            """这一段用第几人称（`exec/33 §10 #80`）。受众只有一个人 ⇒ 用「你」。"""
            return build_person_hint([nicknames[pid] for pid in audience if pid in nicknames])

        async def _with_recall(text: str, audience: frozenset[str] | None) -> str:
            """玩家在打听过去的事时，把原文查回来贴到局面块末尾（`exec/47` P2）。

            🔴 **必须在这里做，不能做成 situation 钩子**：召回要查库（异步），
            而 `SituationBlock.render` 是同步的；更要紧的是它**必须按这一段的
            受众裁**——分头时门厅那段不许召回地下室的原文。这里是唯一同时握着
            受众和一个 await 点的地方。

            🔴 `recall_query` 为空（绝大多数拍）时**一次库都不查**，
            拼出来的局面块与召回之前逐字节一致。
            """
            query = getattr(decision, "recall_query", None)
            if not query:
                return text
            async with self._session_factory() as db:
                hits = await recall_history(db, room_id=room_id, query=query, audience=audience)
            block = format_recall(hits)
            return f"{text}\n\n{block}" if block else text

        if len(groups) <= 1 and not covert_speakers:
            suffix = extra_suffix + _person(tuple(all_ids)) + _bystanders(tuple(all_ids))
            situation = await _with_recall(situation, frozenset(all_ids))
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
                        vocatives=vocatives,
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
                    vocatives=vocatives,
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

        self._split_turn_ordinal += 1
        turn_ordinal = self._split_turn_ordinal

        async def _segment(
            index: int,
            audience: tuple[str, ...],
            node_id: str | None,
            hint: str,
            *,
            covert: bool = False,
        ):
            async with self._session_factory() as db:
                known = await visible_fact_ids(db, room_id=room_id, audience=frozenset(audience))
            nickname, said = _said_by(audience)
            scoped_situation = situation_builder.render(
                audience=frozenset(audience),
                ledger=render_ledger(self._module, known),
                nickname=nickname,
                utterance=said,
                # 🔴 分层注入时这个参数决定叙事能不能看到**玩家刚走到的那个
                # 节点**的正文：局面块整轮只建一次、用的是裁决之前的
                # keeper_state，新落点此刻只存在于 decision 里（`exec/47` P1b）。
                decision=decision,
            )
            scoped_situation = await _with_recall(scoped_situation, frozenset(audience))
            suffix = extra_suffix + hint + _person(audience) + _bystanders(audience)
            # 🔴 磁带子键（`exec/33 §4` 拦路石 1）：并行之后这几次调用的**完成
            # 顺序不确定**，按全局序号回放必然错位。`turn_ordinal` 由分头轮次
            # 递增、`index` 是段落在列表里的位置，两者都由代码算、跟模型输出
            # 无关，所以录制与回放会得到同一个键。
            tape_key = f"narrate-seg:{turn_ordinal}:{index}"
            if segment_delta_sink is not None:
                event_id = str(uuid4())
                text = await self._narrate_prose_streamed(
                    scoped_situation,
                    decision,
                    report,
                    issues,
                    token_limit=token_limit,
                    char_limit=char_limit,
                    extra_suffix=suffix,
                    action_intent=action_intent,
                    confused=confused,
                    room_id=room_id,
                    on_delta=segment_delta_sink(event_id, audience),
                    tape_key=tape_key,
                    vocatives=vocatives,
                )
            else:
                event_id = None
                raw = await self._narrate_prose(
                    scoped_situation,
                    decision,
                    report,
                    issues,
                    max_tokens=token_limit,
                    max_chars=char_limit,
                    extra_suffix=suffix,
                    tape_key=tape_key,
                )
                text = self._finalize_prose(
                    raw,
                    action_intent=action_intent,
                    confused=confused,
                    max_chars=char_limit,
                    room_id=room_id,
                    vocatives=vocatives,
                )
            return NarrationSegment(
                text=text,
                audience=audience,
                node_id=node_id,
                covert=covert,
                event_id=event_id,
            )

        # 🔴 先把每段的协程排好，最后一起 `gather`（`exec/33 §4`）——原来是
        # `for ... await`，第 N 组要等前面 N-1 段**全部写完**。实测量级：裁决
        # ~5s、每段叙事 ~10s，两组串行 25s、并行 15s。**分头场景的大头是串行，
        # 不是流式**（流式只压缩"一段之内"的等待，压不掉"排在前面那几段"）。
        #
        # 并行安全的理由是**它不写世界状态**：段落只读一次线索账本随即关掉
        # session（并发只读实测无锁竞争），写库全在调用方那一侧串行做。
        # 裁决仍然一次且串行——世界状态只有一份。
        planned: list[Awaitable[NarrationSegment]] = []
        for pid in covert_speakers:
            who = nicknames.get(pid, pid)
            planned.append(
                _segment(
                    len(planned),
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
            # 即兴地点也解析得出名字（exec/32）——原来只查剧本节点，人在图外时
            # 这里会把 id 或「此处」写进硬提醒，模型只能照着写"在此处"。
            where = resolve_location(self._module, keeper_state, node_id) or (node_id or "此处")
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
            planned.append(_segment(len(planned), tuple(members), node_id, hint))

        started = time.perf_counter()
        segments: list[NarrationSegment] = list(await asyncio.gather(*planned))
        logger.info(
            "keeper_narration_split",
            room_id=room_id,
            groups=[(nid, len(m)) for nid, m in groups],
            covert_speakers=len(covert_speakers),
            segments=len(segments),
            # 并行有没有真的生效，看这个数：串行是 N×单段耗时，并行接近单段。
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            streamed=segment_delta_sink is not None,
        )
        return "", segments

    def _spawn_chapter_summary(
        self,
        room_id: str,
        history_lines: list[HistoryLine],
        everyone: frozenset[str],
        *,
        scene_changed: bool = True,
    ) -> None:
        """把摘要生成丢到后台。刻意不 await——它不在玩家等待路径上。

        `scene_changed` 透传给 `should_summarize`：换场景是一条触发路径，
        距上次太久是另一条（兜底）。默认 True 保住既有调用方的行为。
        """
        task = asyncio.create_task(
            self._summarize_chapter(room_id, history_lines, everyone, scene_changed=scene_changed)
        )
        # 存一份引用防止任务被 GC 提前回收（asyncio 只持弱引用）
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _summarize_chapter(
        self,
        room_id: str,
        history_lines: list[HistoryLine],
        everyone: frozenset[str] = frozenset(),
        *,
        scene_changed: bool = True,
    ) -> None:
        """整理一段梗概。任何失败都只记日志——它是记忆的锦上添花，不是主路径。

        分头时**每组各摘一段**（`split_history_for_chapters`），落库带受众。
        未分头时就是一段公开的，与本功能上线前一致。多组时串行摘——它在后台，
        没有人在等，而串行不会让并发写 keeper 的库雪上加霜。
        """
        try:
            async with self._session_factory() as db:
                turns = await turns_since_last_chapter(db, room_id=room_id)
                events = await events_since_last_chapter(db, room_id=room_id)
            # 🔴 用真实的 `scene_changed`，不再写死 True。写死的时候，兜底那条
            # 路径就算被调到也会被"当成换了场景"，两条判据分不开。
            if not should_summarize(
                scene_changed=scene_changed,
                turns_since_last=turns,
                events_since_last=events,
            ):
                return
            # 🔴 **只摘「距上次摘要以来」的那一段，不是整个 L3 窗口。**
            #
            # 传进来的 `history_lines` 是整个窗口（`build_situation` 用的那一份）。
            # 喂全量的后果不是"模型不听话"——prompt 说的是「把下面这段游戏历史
            # 压缩成一句话」，而我们给它的就是"从头"，**它从头讲起是正确执行**。
            #
            # 实测（2026-08-23，350 拍单人局）：11 段摘要里前 7 段全是
            # 「调查员从委托人处取得资料，夜查图书馆，驱车北上…」的**同一个故事
            # 的不同长度版本**——不是分段摘要，是 11 个越来越长的全局重述。
            # 而 60 字上限不变 ⇒ 对局越长，每段密度越低，**细节第一个被压掉**：
            # 探针问"借书卡夹在第几页"、"那盏灯的外号"，两条都**编了一个**
            # （47 页 / 歪脖子），语气跟答对的那条一样确信。
            #
            # `events_since_last_chapter` 数的就是 L3 口径的条数（同一个集合），
            # 所以取尾部这么多行 = 距上次摘要以来的那一段。窗口已满时它会大于
            # 窗口长度，切片自然退化成"全部"——那时窗口里本来就只有这么多。
            recent = history_lines[-events:] if events else history_lines
            for audience, lines in split_history_for_chapters(recent, everyone):
                if not lines:
                    continue
                # 🔴 摘多少字，按**这一组实际拿到多少原文**算，不按房间算——
                # 分头时每组看到的行数可以差很多，共用一个预算会让短的那组
                # 被迫注水、长的那组被压扁。
                budget = chapter_budget(sum(len(line) for line in lines))
                text = await summarize_chapter(self._client, lines, budget_chars=budget)
                async with self._session_factory() as db:
                    await record_chapter(
                        db,
                        room_id=room_id,
                        text=text,
                        audience=audience,
                        max_chars=int(budget * 1.5),
                    )
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
        vocatives: frozenset[str] = frozenset(),
    ) -> str:
        scrubbed = scrub_kp_anti_patterns(
            text, action_intent=action_intent, confused=confused, vocatives=vocatives
        )
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
            merge_pending = await pending_decision_manager.player_ids_of_kind(
                db, room_id, MERGE_CONFIRM_KIND
            )
        groups = group_players(keeper_state, ids, merge_pending)
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

            # 🔴 暂离的人**不进在场名单**（`capabilities/presence`）。这一步是
            # 结构性的那一半：名单同时是"叙事里有几个人"和位置分组的来源，
            # 靠 prompt 请它"别提阿福"是纪律性的，而这里是他**根本不在输入里**。
            # 他还在 `players` 表里（回来就恢复），只是这一轮不在场。
            player_rows = [
                p
                for p in (
                    await db.execute(select(Player).where(Player.room_id == room_id))
                ).scalars()
                if not p.away
            ]
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
