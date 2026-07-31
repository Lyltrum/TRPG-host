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

import random
from dataclasses import replace

import structlog
from openai.types.chat import ChatCompletionMessageParam
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.keeper.agenda_state import AGENDA_FIRED_KEY, format_agenda_status, load_fired_agenda
from app.core.keeper.decision import KeeperDecision
from app.core.keeper.module_loader import ScenarioModule
from app.core.keeper.pending import PendingCheck, pending_check_manager
from app.core.keeper.phase import (
    ENDING_ID_KEY,
    PHASE_FINISHED,
    PHASE_KEY,
    PHASE_OPENING,
    format_phase_status,
    load_ending_id,
    load_phase,
)
from app.core.keeper.prompts import (
    build_adjudicator_instructions,
    build_narrator_instructions,
    format_narrator_input,
    format_turn_input,
)
from app.core.keeper.prose_discipline import (
    clip_narration,
    inject_action_resolution_guidance,
    inject_confusion_guidance,
    inject_scene_transition_guidance,
    inject_weird_response_guidance,
    is_clear_action_intent,
    is_player_confused,
    is_violence_edge_utterance,
    is_weird_or_meta_utterance,
    narration_limit,
    narration_max_tokens,
    scrub_kp_anti_patterns,
)
from app.core.keeper.scene_state import CURRENT_NODE_KEY, load_current_node_id
from app.core.keeper.subject import KEEPER
from app.core.keeper.tools import (
    KeeperDeps,
    KeeperToolError,
    roll_check_detail,
    san_check_detail,
    set_phase_impl,
)
from app.core.keeper.turn_executor import create_pending_checks, execute_side_effects
from app.core.keeper.visibility import (
    VISIBILITY_REVEALED_KEY,
    format_visibility_status,
    load_revealed_visibility,
)
from app.core.llm_tape import build_llm_client
from app.core.narrator import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    CheckRequestNotice,
    CheckResultNotice,
    NarrationContext,
    NarrationOutcome,
    Narrator,
)
from app.dto.game import RulesetRead
from app.models.event import Event
from app.models.room import Character, Player, Room

logger = structlog.get_logger()

# 🔴 真人实测 2026-07-28（神秘渡轮）复现：单次请求卡了 4~5 分钟——
# AsyncOpenAI 客户端此前没传 timeout，落到 SDK 默认值（600 秒）。这段时间
# 里玩家发的消息在 WS 收发循环里是同步 await 处理的，连别的操作都发不出去，
# 前端只有一个转圈的"…"，没有任何提示。KeeperAgent 的上下文比
# narrator.py::DeepSeekNarrator（30 秒）大得多——剧本全文常驻 system
# prompt + 完整历史重放，给宽松一点；超时后走既有的宽捕获异常路径
# （ws.py 的 narrator_failed → "守秘人整理思路时卡了一下，请重试"兜底广播），
# 不再无界等待。
_REQUEST_TIMEOUT_SECONDS = 60.0

# 全量重放 events 的上限条数。短模组一场 2-3 小时也就几百条，全放得下
# （DeepSeek 64K 上下文）；上限只是防御异常膨胀的房间。
_HISTORY_LIMIT = 200

# 裁决 JSON 解析失败时的重试次数（把解析错误喂回去让模型改）。
# 空响应常见于模型偶发不返回 content；多给一次机会，仍失败则兜底决策。
_ADJUDICATE_RETRIES = 2

# 🔴 deepseek-v4-pro 默认开启隐藏推理（message.reasoning_content），推理 token
# 与可见正文共享同一个 max_tokens 预算——实测简单请求推理耗 77~119 token，复杂
# 真实局面耗更多，曾把正文预算挤到只剩 51 字就被硬砍断（真人实测 2026-07-28
# 复现）。裁决/叙事两阶段的设计（低温稳定输出 / 固定字数上限）都是在
# deepseek-chat（无隐藏推理）上定的，v4-pro 的隐藏推理与这套预算模型不兼容，
# 关掉即可——本项目不需要模型的链式思考，需要判断力的地方已经是独立的裁决
# 阶段。已用真实请求验证：加此参数后 reasoning_tokens 归零，正文按预算完整生成。
_DISABLE_THINKING: dict = {"thinking": {"type": "disabled"}}

_FALLBACK_ADJUDICATE_GUIDANCE = (
    "【系统兜底】裁决模型未返回合法 JSON。"
    "请用一两句世界内文字回应玩家本轮意图：能推进就写行动结果，"
    "有障碍就写眼前障碍；不要编造未发生的重大剧情；"
    "可请玩家用更明确的一句行动再说一次。checks 必须为空。"
)

# 历史重放里守秘人旧叙事的截断长度。不截断的话历史里全是它自己的 300-550 字
# 长篇，模型会模仿自己的旧文风越写越长（自我强化）；重要事实的长期记忆靠
# keeper_state 状态笔记承担，历史行只需要"发生过什么"的梗概。
_HISTORY_NARRATION_CLIP = 160

# 事件类型 → 历史行格式化器。keeper.state 不进历史（状态笔记单独整体注入），
# 工具留痕（检定/HP/San）进历史是为了让守秘人记得自己此前的裁决结果。
_EVENT_LABELS = {
    "keeper.check": "检定",
    "keeper.san": "理智",
    "keeper.hp": "生命",
}


def _pending_to_notice(pending: PendingCheck) -> CheckRequestNotice:
    return CheckRequestNotice(
        check_request_id=pending.check_request_id,
        kind=pending.kind,
        player_id=pending.player_id,
        player_nickname=pending.player_nickname,
        skill=pending.skill,
        reason=pending.reason,
    )


def _build_check_boundary_hint(pending_checks: list[PendingCheck]) -> str:
    """检定边界硬提醒（真人实测 2026-07-29：恐吓/追踪/潜行三个真实案例）。

    旧版指引只禁止"提前描写检定才能获得的信息"（信息维度），真人实测
    发现追踪/潜行两个案例抢跑的不是信息，是**检定对应的动作本身**——
    铺垫文字已经把"沿着痕迹走了十几步""脚步声被夜风吞掉"这类只有检定
    成功才该出现的执行过程当成既定事实写出来了。这里补齐这第二个维度，
    用一个技能无关的通用例子锚定理解，不针对具体技能列举——列举会重蹈
    exec/11 已经记录过的"样本驱动模式匹配，泛化边界不可知"的坑。
    """
    check_list = "\n".join(
        f"- {c.player_nickname} · {'理智' if c.kind == 'san' else c.skill}检定"
        f"（{c.reason or '无说明'}）"
        for c in pending_checks
    )
    return (
        "\n\n【检定边界·代码硬提醒】本轮已发起以下检定，正文必须停在"
        "「结果与动作都还未知」的那一刻：\n"
        f"{check_list}\n"
        "① 不得写出这次检定才能揭示的信息（线索/证词/发现），哪怕一个字；\n"
        "② 不得把这次检定对应的动作本身写成已经在成功进行/已经执行完成——"
        "比如「沿着痕迹走了很远」「脚步声被夜风吞掉」「三十步后你看见了」"
        "这类，都已经是在替这次检定预支结果；\n"
        "③ 正确的停点是「你看见一条隐约的痕迹」「你压低身子准备靠近」"
        "这种，情境刚具备、行动才要开始、结果完全悬而未决的画面。\n"
        "超出这个边界会显得逻辑混乱——检定还没掷，故事却已经替玩家决定了结果。"
    )


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
            api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=_REQUEST_TIMEOUT_SECONDS
        )
        self._adjudicator_instructions = build_adjudicator_instructions(module, ruleset)
        self._narrator_instructions = build_narrator_instructions(module)

    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        if context.room_id is None or context.player_id is None:
            raise ValueError("KeeperAgent 需要 NarrationContext 携带 room_id/player_id")
        room_id = context.room_id
        is_heartbeat = getattr(context, "is_heartbeat", False)
        is_opening_ceremony = getattr(context, "is_opening_ceremony", False)

        # 两段式玩家掷骰：还有待掷的检定时不再裁决新一轮——先让玩家把手头的
        # 骰子掷完。重发同一个请求（而不是静默不回应），防前端刷新丢卡片。
        # 主动心跳 / 开场仪式：有待掷时直接放弃（开场不该卡在旧检定上）。
        pending = pending_check_manager.first(room_id)
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
                check_requests=[_pending_to_notice(pending)],
            )

        keeper_state, history_lines, roster = await self._load_room_memory(room_id)

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
                rng=self._rng,
            )
            await set_phase_impl(deps_boot, PHASE_OPENING)
            phase = PHASE_OPENING
            keeper_state = {
                **(keeper_state or {}),
                PHASE_KEY: PHASE_OPENING,
            }

        # 议程 / 密级 / 阶段状态由代码注入——once 与揭开记账不靠模型自觉。
        fired = load_fired_agenda(keeper_state)
        agenda_status = format_agenda_status(self._module, fired)
        revealed = load_revealed_visibility(keeper_state)
        visibility_status = format_visibility_status(
            self._module, revealed, observer_id=context.player_id
        )
        phase_status = format_phase_status(phase, ending_id)
        _hidden_keys = {
            AGENDA_FIRED_KEY,
            VISIBILITY_REVEALED_KEY,
            PHASE_KEY,
            ENDING_ID_KEY,
            CURRENT_NODE_KEY,
        }
        visible_state = (
            {k: v for k, v in keeper_state.items() if k not in _hidden_keys}
            if keeper_state
            else keeper_state
        )
        situation = format_turn_input(
            visible_state,
            history_lines,
            roster,
            context.player_nickname,
            context.utterance,
            agenda_status=agenda_status,
            visibility_status=visibility_status,
            phase_status=phase_status,
            is_heartbeat=is_heartbeat,
            is_opening_ceremony=is_opening_ceremony,
            phase=phase,
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
                    narration, action_intent=False, confused=False, max_chars=char_limit
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
        # 主动轮 / 开场仪式硬约束：丢弃检定请求（设计：开场不发起高风险检定）
        if (is_heartbeat or is_opening_ceremony) and (decision.checks or decision.san_checks):
            decision = decision.model_copy(update={"checks": [], "san_checks": []})

        # 迷茫 / 怪话 / 明确行动：代码注入 guidance（不靠模型自觉）。
        # 🔴 2026-07-29：分类信号从正则改为裁决 LLM 在同一次调用里顺手给出的
        # player_state 字段——正则要求关键词字面严格相邻（如"我该"必须紧邻），
        # 真人实测"我现在该做什么"（插了"现在"）就匹配不上，这是正则做语义
        # 分类的结构性上限，不是"这条正则不够全"。只有裁决完全失败（走
        # _FALLBACK_ADJUDICATE_GUIDANCE 兜底、此时 player_state 只是默认值
        # "normal"、不可信）才退回正则作为兜底安全网。
        is_adjudicate_fallback = decision.narration_guidance == _FALLBACK_ADJUDICATE_GUIDANCE
        if is_adjudicate_fallback:
            confused = is_player_confused(context.utterance)
            weird = is_weird_or_meta_utterance(context.utterance)
            action_intent = is_clear_action_intent(context.utterance)
        else:
            confused = decision.player_state == "confused"
            weird = decision.player_state == "weird_or_meta"
            action_intent = decision.player_state == "clear_action"
        if confused:
            # 🔴 裁决走兜底（_FALLBACK_ADJUDICATE_GUIDANCE）时不要把它和迷茫引导拼
            # 一起——兜底文案说"别编造+可请玩家重说一遍"，迷茫引导说"必须给 1-2
            # 个具体方向"，两句话方向相反，叙事模型会各退一步、缩回复述已知信息
            # 这个最安全选项（真人实测 2026-07-28 复现：玩家问"该做什么"，回复是
            # 前情复述而非建议）。迷茫引导本身已自洽（给方向不需要先问清楚），
            # 兜底走这条分支时直接丢弃、不拼接。
            base_guidance = "" if is_adjudicate_fallback else decision.narration_guidance
            decision = decision.model_copy(
                update={
                    "checks": [],
                    "san_checks": [],
                    "narration_guidance": inject_confusion_guidance(base_guidance),
                }
            )
        elif weird and not is_heartbeat and not is_opening_ceremony:
            # 怪话接招：元/玩笑清检定；暴力边界保留检定（伤害/SAN）但同样强制接招
            update: dict = {
                "narration_guidance": inject_weird_response_guidance(decision.narration_guidance),
            }
            if not is_violence_edge_utterance(context.utterance):
                update["checks"] = []
                update["san_checks"] = []
            decision = decision.model_copy(update=update)
        elif action_intent and not is_heartbeat and not is_opening_ceremony:
            # 明确行动：强制推进，禁止街景挡枪（全模组通用）
            decision = decision.model_copy(
                update={
                    "narration_guidance": inject_action_resolution_guidance(
                        decision.narration_guidance
                    ),
                }
            )

        # 场景切换：独立于上面迷茫/怪话/明确行动三选一，两者可叠加生效。
        # 判断信号是「当前场景」这个字段本身的变化（上一轮 keeper_state 里的
        # 旧值 vs 这轮裁决刚写入的新值），不靠模型自己判断"是不是在对话中途
        # 离场"——覆盖所有位置跳变，不止离开 NPC 对话这一种。真人实测
        # 2026-07-29：玩家还在跟邻居对话，宣告去书房，回复直接是"钥匙已经
        # 转了半圈、门已经推开"，完全跳过了道别+赶路这段，读起来像瞬移。
        # 心跳/开场仪式各自已有独立的内容约束，跳过这条。
        #
        # 🔴 2026-07-30（04 遗留项）：优先用结构化的 current_node_id 做精确
        # 比较——此前只比较「当前场景」自由文本，同一地点换个措辞（"书房"
        # vs "惠特利宅书房"）会被误判成切换。双方都有 node_id 时以它为准；
        # 否则退回自由文本比较（兼容尚未产出 node id 的模组/历史房间）。
        prev_scene = (keeper_state or {}).get("当前场景")
        new_scene = next((u.value for u in decision.state_updates if u.key == "当前场景"), None)
        prev_node_id = load_current_node_id(keeper_state)
        new_node_id = decision.current_node_id
        if prev_node_id is not None and new_node_id is not None:
            scene_changed = prev_node_id != new_node_id
        else:
            scene_changed = (
                prev_scene is not None and new_scene is not None and prev_scene != new_scene
            )
        if not is_heartbeat and not is_opening_ceremony and scene_changed:
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
            checks=[c.skill for c in decision.checks],
            san_checks=len(decision.san_checks),
            hp_changes=len(decision.hp_changes),
            state_updates=[u.key for u in decision.state_updates],
            agenda_fired=decision.agenda_fired,
            visibility_revealed=decision.visibility_revealed,
            opening_complete=decision.opening_complete,
            ending_reached=decision.ending_reached,
            is_heartbeat=is_heartbeat,
            is_opening_ceremony=is_opening_ceremony,
            player_confused=confused,
            clear_action_intent=action_intent,
            weird_or_meta=weird,
            scene_transition=scene_changed,
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
            rng=self._rng,
        )
        # 守秘人的身份显式传进去：它不是"唯一那条代码路径"，是一个视图取
        # 全集、持全权限的主体（exec/14 P2）。全权限下 sanitize/authorize 都
        # 是恒等操作，行为与此前逐字节一致。
        report, issues = await execute_side_effects(deps, decision, subject=KEEPER)
        pending_checks, pending_issues = await create_pending_checks(deps, decision, subject=KEEPER)
        issues = [*issues, *pending_issues]

        if pending_checks:
            pending_check_manager.add(room_id, pending_checks)
            # 🔴 真人实测 2026-07-29：检定发起前的铺垫文字，不止会提前泄露
            # 检定结果（"东北角矮墓碑"这类招供内容），还会提前把检定对应的
            # 动作本身写成已经在成功进行（追踪检定前先写"沿着小径走了十几
            # 步"、潜行检定前先写"脚步声被夜风吞掉""三十步后你看见了"）——
            # 这是同一类问题的两个维度，旧版指引只堵了"信息"这一维。
            # 这段硬提醒改放在 user_content 最末尾（仿 length_hint 的位置，
            # 近因效应下模型服从概率更高），不再折进 narration_guidance
            # 中段——没法用代码保证模型一定服从（"这段话有没有替检定预支
            # 结果"不是能靠代码判断的），只能尽量提高服从概率。
            check_boundary_hint = _build_check_boundary_hint(pending_checks)
            narration = await self._narrate_prose(
                situation,
                decision,
                report,
                issues,
                max_tokens=token_limit,
                max_chars=char_limit,
                extra_suffix=check_boundary_hint,
            )
            narration = self._finalize_prose(
                narration,
                action_intent=action_intent,
                confused=confused,
                max_chars=char_limit,
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
                check_requests=[_pending_to_notice(c) for c in pending_checks],
                stat_changes=deps.stat_changes,
            )

        # 阶段3·叙事：只写故事 + 长度硬裁 + 去菜单/软挡。
        narration = await self._narrate_prose(
            situation,
            decision,
            report,
            issues,
            max_tokens=token_limit,
            max_chars=char_limit,
        )
        narration = self._finalize_prose(
            narration,
            action_intent=action_intent,
            confused=confused,
            max_chars=char_limit,
        )

        # HP 变化的可见性不再靠拼进叙事正文保证——那样等于让守秘人的嘴说了句
        # 不该它说的系统台词（真人实测 2026-07-28 反馈）。现在 deps.stat_changes
        # 走 character.stat_changed 结构化广播，前端渲染成独立的系统提示，
        # 和叙事气泡分开。
        return NarrationOutcome(text=narration, stat_changes=deps.stat_changes)

    async def resolve_check(
        self, room_id: str, player_id: str, check_request_id: str
    ) -> NarrationOutcome:
        """结算一次玩家确认的掷骰（两段式玩家掷骰）。

        队列还没清空：只广播这次的结果，不叙事——等玩家把本轮所有待掷检定
        都掷完。队列清空：复用 `narrate()` 触发一轮"结算叙事"——裁决器能
        在历史（keeper.check/keeper.san 事件）里看到刚掷出的结果，据此裁决
        后续（可能链式追加新的检定，比如目击后的理智检定，自然进入下一轮
        pending）。
        """
        pending = pending_check_manager.pop(room_id, check_request_id)
        if pending is None:
            raise KeeperToolError("没有这个待掷的检定（可能已被结算）")
        if pending.player_id != player_id:
            pending_check_manager.requeue_front(room_id, pending)
            raise KeeperToolError(f"这个检定应由 {pending.player_nickname} 来掷")

        deps = KeeperDeps(
            room_id=room_id,
            player_id=pending.player_id,
            session_factory=self._session_factory,
            module=self._module,
            ruleset=self._ruleset,
            rng=self._rng,
        )
        if pending.kind == "skill":
            assert pending.skill is not None
            _text, detail = await roll_check_detail(deps, pending.skill, pending.player_nickname)
            notice = CheckResultNotice(
                check_request_id=pending.check_request_id,
                kind="skill",
                player_id=detail["player_id"],
                skill=detail["skill"],
                rolled=detail["rolled"],
                target=detail["target"],
                level=detail["level"],
            )
        else:
            _text, detail = await san_check_detail(
                deps, pending.loss_on_success, pending.loss_on_failure, pending.player_nickname
            )
            notice = CheckResultNotice(
                check_request_id=pending.check_request_id,
                kind="san",
                player_id=detail["player_id"],
                skill=None,
                rolled=detail["rolled"],
                target=detail["target"],
                level="成功" if detail["succeeded"] else "失败",
                san_loss=detail["loss"],
                san_remaining=detail["san"],
            )

        logger.info(
            "keeper_check_resolved",
            room_id=room_id,
            check_request_id=check_request_id,
            kind=pending.kind,
            player=pending.player_nickname,
        )

        next_pending = pending_check_manager.first(room_id)
        if next_pending is not None:
            return NarrationOutcome(
                text="",
                check_results=[notice],
                check_requests=[_pending_to_notice(next_pending)],
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
        """阶段1：裁决。JSON mode + pydantic 校验，解析失败把错误喂回去重试。

        温度压低（0.3）：裁决要的是稳定一致的规则判断，不是创造力。
        全部重试仍失败（含空 content）时返回兜底决策，避免整轮静默失败。
        """
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": self._adjudicator_instructions},
            {"role": "user", "content": situation + "\n\n请输出本轮的裁决 JSON。"},
        ]
        last_error: Exception | None = None
        for attempt in range(1 + _ADJUDICATE_RETRIES):
            response = await self._client.chat.completions.create(
                tape_kind="adjudicate",
                model=DEEPSEEK_MODEL,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.3,
                extra_body=_DISABLE_THINKING,
            )
            raw = (response.choices[0].message.content or "").strip()
            if not raw:
                last_error = ValueError("empty adjudicate response")
                logger.warning(
                    "keeper_adjudicate_empty",
                    attempt=attempt + 1,
                    room_hint=situation[:80],
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "你上一轮返回了空内容。必须输出完整合法的裁决 JSON 对象，"
                            "至少包含 thinking 与 narration_guidance 字段；"
                            "checks/san_checks/hp_changes/state_updates 可为 []。"
                        ),
                    }
                )
                continue
            try:
                return KeeperDecision.model_validate_json(raw)
            except ValidationError as exc:
                last_error = exc
                messages.append({"role": "assistant", "content": raw})
                messages.append(
                    {
                        "role": "user",
                        "content": f"JSON 不符合要求：{exc}。请重新只输出一个合法的裁决 JSON。",
                    }
                )
        logger.warning(
            "keeper_adjudicate_fallback",
            error=str(last_error),
        )
        return KeeperDecision(
            thinking=f"裁决解析失败兜底：{last_error}",
            narration_guidance=_FALLBACK_ADJUDICATE_GUIDANCE,
        )

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
        """阶段3：叙事。max_tokens 限生成；max_chars 代码硬裁（句末优先）。

        `extra_suffix` 追加在 `length_hint` 之后、整段 user_content 的最
        末尾——目前唯一的用途是检定边界硬提醒（见 `_build_check_boundary_
        hint`），放在这个位置是刻意的（近因效应，模型对最后读到的指令
        服从概率更高，跟 `length_hint` 本身的位置选择同理）。
        """
        length_hint = (
            f"\n\n【长度硬限】本轮正文不得超过 {max_chars} 字（含标点）。"
            "超长会被系统截断——请一次写完且写短。"
        )
        user_content = (
            format_narrator_input(situation, decision.narration_guidance, report, issues)
            + length_hint
            + extra_suffix
        )
        response = await self._client.chat.completions.create(
            tape_kind="narrate",
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": self._narrator_instructions},
                {"role": "user", "content": user_content},
            ],
            temperature=0.8,
            max_tokens=max_tokens,
            extra_body=_DISABLE_THINKING,
        )
        raw = response.choices[0].message.content or ""
        if response.choices[0].finish_reason == "length":
            # 即便关了隐藏推理，正文本身也可能写超——这里留痕方便以后一眼看出
            # 是被 max_tokens 硬砍的，而不是 clip_narration 的优雅裁切。
            logger.warning(
                "keeper_narration_hit_token_limit",
                max_tokens=max_tokens,
                raw_len=len(raw.strip()),
            )
        clipped = clip_narration(raw, max_chars)
        if len(raw.strip()) > max_chars:
            logger.info(
                "keeper_narration_clipped",
                before=len(raw.strip()),
                after=len(clipped),
                limit=max_chars,
            )
        return clipped

    def _finalize_prose(
        self,
        text: str,
        *,
        action_intent: bool,
        confused: bool = False,
        max_chars: int,
    ) -> str:
        scrubbed = scrub_kp_anti_patterns(text, action_intent=action_intent, confused=confused)
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

    async def _load_room_memory(self, room_id: str) -> tuple[dict | None, list[str], list[str]]:
        """读取世界状态笔记 + 全量事件历史 + 在场调查员名单。

        与 build_narration_context 的 6 条窗口不同：守秘人要对整局的一致性
        负责（玩家在第 3 轮说过的话第 30 轮还得作数），所以重放完整历史。

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
            roster = [
                f"{p.nickname}"
                + (
                    f"（角色：{c.name}，{c.occupation or '无职业'}）"
                    if (c := chars_by_player.get(p.id)) is not None and c.name
                    else "（未建卡）"
                )
                for p in player_rows
                if not p.is_ai
            ]

            result = await db.execute(
                select(Event)
                .where(
                    Event.room_id == room_id,
                    Event.event_type.in_(
                        ["action.submit", "narration.push", *_EVENT_LABELS.keys()]
                    ),
                )
                .order_by(Event.created_at.desc(), Event.id.desc())
                .limit(_HISTORY_LIMIT)
            )
            events = list(result.scalars())
            events.reverse()

            # 历史行的昵称直接用上面已查出的成员表（老成员退出房间的场景本期
            # 不存在，player_rows 就是全量）。
            nicknames = {p.id: p.nickname for p in player_rows}

        lines: list[str] = []
        for event in events:
            payload = event.payload or {}
            if event.event_type == "action.submit":
                who = nicknames.get(event.player_id or "", "玩家")
                lines.append(f"{who}：{payload.get('utterance', '')}")
            elif event.event_type == "narration.push":
                text = payload.get("text", "")
                if len(text) > _HISTORY_NARRATION_CLIP:
                    text = text[:_HISTORY_NARRATION_CLIP] + "……"
                lines.append(f"守秘人：{text}")
            elif event.event_type == "keeper.check":
                # 🔴 2026-07-30（exec/11 待办2）：曾用 `[检定] 玩家 技能：a/b → 结果`
                # 这种方括号"记账行"格式喂给叙事 LLM 看历史，真人实测复现过
                # 叙事正文里编造出一句格式几乎一样但数值全假的"记账"（见
                # prose_discipline.py 的 _FAKE_STAT_LOG_LEAK）——模型照猫画虎
                # 模仿了这里看到的模板。改成普通叙述句，不留可逐字复刻的模板。
                lines.append(
                    f"{payload.get('player', '')}进行了一次{payload.get('skill', '')}"
                    f"检定，掷出{payload.get('rolled', '?')}，目标"
                    f"{payload.get('target', '?')}，结果{payload.get('level', '')}。"
                )
            elif event.event_type == "keeper.san":
                lines.append(
                    f"{payload.get('player', '')}遭受理智冲击，损失"
                    f"{payload.get('loss', '?')}点理智，当前理智值{payload.get('san', '?')}。"
                )
            elif event.event_type == "keeper.hp":
                lines.append(
                    f"{payload.get('player', '')}的生命值发生变化："
                    f"{payload.get('delta', '?')}点（{payload.get('reason', '')}），"
                    f"当前生命值{payload.get('hp', '?')}。"
                )
        return keeper_state, lines, roster
