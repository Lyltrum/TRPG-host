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
from collections.abc import Callable
from dataclasses import replace

import structlog
from openai.types.chat import ChatCompletionMessageParam
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.keeper.agenda_state import AGENDA_FIRED_KEY, format_agenda_status, load_fired_agenda
from app.core.keeper.capabilities import audit_fields, situation_blocks
from app.core.keeper.chapter import (
    load_chapters,
    record_chapter,
    render_chapters,
    should_summarize,
    turns_since_last_chapter,
)
from app.core.keeper.decision import KeeperDecision
from app.core.keeper.deps import KeeperDeps, KeeperToolError
from app.core.keeper.dice import is_success
from app.core.keeper.fact_ledger import (
    record_revelations,
    render_ledger,
    revealed_fact_ids,
    visible_fact_ids,
)
from app.core.keeper.history import (
    HISTORY_EVENT_TYPES,
    HISTORY_LIMIT,
    HistoryLine,
    history_lines_from_events,
    visible_history,
)
from app.core.keeper.leak_guard import log_leak_hits, scrub_meta_leaks
from app.core.keeper.location_state import (
    PLAYER_LOCATION_KEY,
    format_party_locations,
    group_players,
    load_hidden_players,
    location_of,
)
from app.core.keeper.module_loader import ScenarioModule
from app.core.keeper.pending import PendingCheck, pending_check_manager, to_notice
from app.core.keeper.phase import (
    ENDING_ID_KEY,
    PHASE_FINISHED,
    PHASE_KEY,
    PHASE_OPENING,
    format_endings_status,
    format_phase_status,
    load_ending_id,
    load_phase,
)
from app.core.keeper.prompts import (
    CHAPTER_SUMMARY_INSTRUCTIONS,
    build_adjudicator_instructions,
    build_narrator_instructions,
    format_chapter_input,
    format_narrator_input,
    format_turn_input,
)
from app.core.keeper.prose_discipline import (
    clip_narration,
    inject_action_resolution_guidance,
    inject_confusion_guidance,
    inject_feasibility_question_guidance,
    inject_kp_question_guidance,
    inject_scene_transition_guidance,
    inject_spotlight_guidance,
    inject_weird_response_guidance,
    is_clear_action_intent,
    is_player_confused,
    is_violence_edge_utterance,
    is_weird_or_meta_utterance,
    narration_limit,
    narration_max_tokens,
    scrub_kp_anti_patterns,
)
from app.core.keeper.scene_state import CURRENT_NODE_KEY
from app.core.keeper.sheet_digest import format_sheet
from app.core.keeper.subject import KEEPER
from app.core.keeper.tools import (
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
from app.core.narration.contract import (
    CheckResultCallback,
    CheckResultNotice,
    NarrationContext,
    NarrationOutcome,
    NarrationSegment,
    Narrator,
    PlayerUtterance,
)
from app.core.narration.deepseek import DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
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


# 本轮**没有**任何待掷检定时由代码强制追加。跟 `_build_check_boundary_hint`
# 是同一件事的另一半：那个管"有检定时别抢跑"，这个管"没检定时别凭空要求掷骰"。
#
# 真人实测 2026-07-31（exec/19 #38）：裁决输出 `checks=[]`，叙事却写出「凌铭辉，
# 进行一次体质对抗检定，目标 POT 16」——玩家界面上永远不会出现那张掷骰卡片，
# 他就一直等下去。根因是**对抗检定在 `KeeperDecision` 里无法表达**（`CheckRequest`
# 只有 skill/player/reason，没有对抗目标值），裁决器想要就只能写进散文里。
#
# ⚠️ 这只是止血，是**概率性**的：真正的修法是让 schema 表达得了对抗检定，
# 否则模型永远有动机把说不出口的东西塞进正文（与 exec/17 同族）。
_NO_PENDING_CHECK_HINT = (
    "\n\n【检定纪律·代码硬提醒】本轮**没有任何待掷检定**。因此正文里"
    "**不得要求任何调查员掷骰或进行检定**——不要写「请进行 XX 检定」"
    "「目标值 XX」「掷一次 XX」这类话。玩家界面上不会出现掷骰卡片，"
    "写了他只会一直等一个永远不来的骰子。"
    "需要不确定性时，直接把结果写成既定事实，或者把局面停在他下一步可以行动的地方。"
)


# 🔴 上面那条硬提醒有个例外，试玩实测抓到（2026-07-31，exec/19 #44）：
# 玩家宣告「我抄起手边的东西狠狠砸它」，裁决器没发检定，叙事照着
# `_NO_PENDING_CHECK_HINT` 的"直接把结果写成既定事实"写了「他侧身一闪，
# 镇纸砸在书架上」——**攻击的成败被叙事定了**。
#
# 搜查、移动、对话那些动作，没检定就直接给结果是对的；但**对他人动手**不行：
# 攻击的成败是玩家花技能点买来的权利，不该由叙事赠予或剥夺。裁决器没能发出
# 检定（目标指代不明、局面不清）时，正确处理是**停下来追问**，不是替他裁定。
#
# 触发条件是**确定的**：`player_state == "physical_conflict"` 且本轮 pending
# 为空。分类本身是语义判断（裁决 LLM 做，同 #8 迷茫检测、#40 提问的先例），
# 但"分类命中 + 没有检定 → 注入这段"这一步是代码强制的。
_UNRESOLVED_CONFLICT_HINT = (
    "\n\n【动手未裁定·代码硬提醒】玩家本轮宣告了对他人动手/强行突破，"
    "而本轮**没有任何检定**。因此：\n"
    "① **绝对不许写出这次动手的成败**——不许写打中、打空、被闪开、被制住、"
    "被夺下武器、对方受伤或毫发无伤；\n"
    "② 正文停在**动作即将发生的那一刻**（他已经抬手/已经扑过去，结果未知），"
    "然后用一句世界内的话向玩家确认他到底要对谁做什么"
    "（「你是要动手打他？」「你打算硬闯过去？」）；\n"
    "③ 不要要求掷骰、不要提检定二字——确认清楚后由守秘人下一轮裁决。\n"
    "攻击的成败是玩家用技能点换来的权利，不能由叙事赠予或剥夺。"
)


def _build_bystander_hint(nicknames: list[str]) -> str:
    """本轮没发言的人：点名禁止替他们行动（exec/19 #41）。

    真人实测 2026-07-31：只有凌铭辉提交了行动，叙事却写「张家豪扫了一眼鞋柜旁
    那双沾泥的雨靴」——张家豪什么都没说。叙事 prompt 里本来就有「不替玩家决定
    下一步」，但那是**没有名单的泛化纪律**；名册上摆着另一个名字、聚光灯指引又
    在鼓励照顾镜头，模型自然会给他补一笔。代码明明知道谁发了言、谁没发言，
    这里把这份名单交出去。

    ⚠️ 名单是代码确定的，**模型服从与否仍是概率性的**（同 `_NO_PENDING_CHECK_HINT`）。
    ⚠️ 名单必须按**每段的受众**算，不能把别组的人名带进来——否则 per-observer
    投递做的隔离会被这条 prompt 自己泄回去。
    """
    if not nicknames:
        return ""
    names = "、".join(nicknames)
    return (
        f"\n\n【发言人名单·代码硬提醒】本轮只有已列出的调查员提交了行动。"
        f"{names}**这一轮什么都没说**——不得替他{'们' if len(nicknames) > 1 else ''}"
        "写出任何主动动作、主动观察或台词（不要写他去看了什么、注意到了什么、"
        "说了什么）。只能写他被动在场（站在那里、跟着走），或者让环境/NPC 朝他"
        "抛一个钩子，把下一步留给他自己决定。"
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
                turn_player_ids=turn_player_ids,
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
            # 逐人位置是 `player_id@node_id` 的机器格式，对 LLM 无意义——
            # 它看到的是下面渲染好的「各自所在」（P5.2）。
            PLAYER_LOCATION_KEY,
        }
        visible_state = (
            {k: v for k, v in keeper_state.items() if k not in _hidden_keys}
            if keeper_state
            else keeper_state
        )
        # 事实账本 L1：读全量（不设 limit）——它必须活过 HISTORY_LIMIT 的
        # 200 条滑动窗口，这正是它存在的理由。
        async with self._session_factory() as db:
            known_facts = await revealed_fact_ids(db, room_id=room_id)
            chapters = await load_chapters(db, room_id=room_id)
        ledger_status = render_ledger(self._module, known_facts)
        chapters_status = render_chapters(chapters)
        # 分头探索（P5.2）：全队同处一地时是空串，整块不渲染。
        locations_status = format_party_locations(self._module, keeper_state, players)
        # 已经垂直切出去的能力要摆在模型眼前的状态（exec/27 阶段 2）：
        # 目前是 health 的「NPC 当前状态」。没记过账时该块整块不渲染。
        # 🔴 这个钩子是切 health 时才发现漏掉的——能力不只要能改世界，还得让
        # 模型**看见**自己改成了什么样，否则下一轮只能从上一段散文里猜。
        capability_status = situation_blocks(self._module, keeper_state)
        # 可能的结局（exec/19 #47）：与议程同一待遇，每轮摆在裁决器眼前。
        endings_status = format_endings_status(self._module)

        def build_situation(
            *,
            audience: frozenset[str] | None,
            ledger: str,
            nickname: str,
            utterance: str,
        ) -> str:
            """按受众组装局面块（exec/14 P5.2d）。

            `audience=None` = 守秘人视图（裁决阶段用）：历史与账本全给。
            分组叙事时传该组的受众，历史/账本/原话三处一起裁——**模型拿不到
            的东西才是真的漏不出来**，这比在 prompt 末尾请它别说可靠。
            """
            return format_turn_input(
                visible_state,
                visible_history(history_lines, audience),
                roster,
                nickname,
                utterance,
                agenda_status=agenda_status,
                visibility_status=visibility_status,
                phase_status=phase_status,
                ledger_status=ledger,
                chapters_status=chapters_status,
                locations_status=locations_status,
                capability_blocks=capability_status,
                endings_status=endings_status,
                is_heartbeat=is_heartbeat,
                is_opening_ceremony=is_opening_ceremony,
                phase=phase,
            )

        situation = build_situation(
            audience=None,
            ledger=ledger_status,
            nickname=context.player_nickname,
            utterance=context.utterance,
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
        # 玩家本轮对人动手/强行突破（exec/19 #44）。真正的注入条件还要加上
        # "本轮没裁出任何检定"，那要等 create_pending_checks 跑完才知道，
        # 所以这里只先记住分类。
        physical_conflict = (
            not is_adjudicate_fallback
            and decision.player_state == "physical_conflict"
            and not is_heartbeat
            and not is_opening_ceremony
        )
        # 🔴 真人实测 2026-07-31（exec/19 #40）：玩家问「科比特先生在家吗」，
        # 问的是守秘人（他忘了这个设定），叙事却把它演成角色在门厅里喊话、
        # 还照常推进了场景。提问不是行动——这里代码强制把推进世界的手段全部
        # 收走（检定/移动/场景指针），只留"回答"这一件事。
        #
        # 🔴 2026-08-01（exec/25 #59）：`feasibility_question` 走**同一条**分支。
        # 玩家问「我们能直接去他的地下室吗」被分成了 `clear_action`，叙事直接
        # 推进了剧情——裁决器自己的 thinking 写着"需判断可行性并推进行动"，
        # 它知道玩家在问可行性，但六个格子里没有这一类。
        #
        # 加一格而不是拓宽 `question_to_kp` 的定义：**枚举的格子是给模型分类
        # 用的，不是给代码分支用的**。硬把"征询许可"塞进"回忆已知设定"，那个
        # 类别就变成"要么…要么…"，而含混的定义本身就是分类错的来源。两格行为
        # 相同不是问题，定义能不能写干净才是目的（同 exec/17 的判据）。
        asks_kp = (
            not is_adjudicate_fallback
            and not is_heartbeat
            and not is_opening_ceremony
            and decision.player_state in ("question_to_kp", "feasibility_question")
        )
        feasibility = asks_kp and decision.player_state == "feasibility_question"
        kp_question = asks_kp and not feasibility
        if asks_kp:
            # 收走推进世界的手段：两类共用。guidance 分开——见
            # prose_discipline 里那两段 prefix 的注释。
            inject = (
                inject_feasibility_question_guidance if feasibility else inject_kp_question_guidance
            )
            decision = decision.model_copy(
                update={
                    "checks": [],
                    "san_checks": [],
                    "moves": [],
                    "current_node_id": None,
                    "narration_guidance": inject(decision.narration_guidance),
                }
            )
        elif confused:
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

        # 聚光灯（exec/14 P5.2）：导演层算出"谁最久没被点到"，这里强制注入。
        # 与上面三选一叠加生效——被冷落跟他说的那句话是什么类型无关。
        if context.spotlight_nickname:
            decision = decision.model_copy(
                update={
                    "narration_guidance": inject_spotlight_guidance(
                        decision.narration_guidance, context.spotlight_nickname
                    ),
                }
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
        await self._record_decision(
            room_id=room_id,
            player_id=context.player_id,
            decision=decision,
            forced=[
                name
                for name, hit in (
                    ("kp_question", kp_question),
                    ("feasibility_question", feasibility),
                    ("confused", confused),
                    ("weird_or_meta", weird),
                    ("clear_action", action_intent),
                    ("physical_conflict", physical_conflict),
                    ("adjudicate_fallback", is_adjudicate_fallback),
                    ("spotlight", bool(context.spotlight_nickname)),
                )
                if hit
            ],
        )

        report, issues = await execute_side_effects(deps, decision, subject=KEEPER)
        pending_checks, pending_issues = await create_pending_checks(deps, decision, subject=KEEPER)
        issues = [*issues, *pending_issues]

        # 场景切换：独立于上面迷茫/怪话/明确行动三选一，两者可叠加生效。
        # 真人实测 2026-07-29：玩家还在跟邻居对话，宣告去书房，回复直接是
        # "钥匙已经转了半圈、门已经推开"，跳过了道别+赶路，读起来像瞬移。
        # 心跳/开场仪式各自已有独立的内容约束，跳过这条。
        #
        # 🔴 P5.2：判据从"房间级「当前场景」字段变了没有"改成**逐人位置**
        # 比对——分头探索后房间不再有单一"当前场景"，而"谁挪了窝"本来就
        # 是按人问的问题。因此改成读**执行之后**的状态（位置由 tools 写库，
        # 不是从 decision 字段猜），这也顺带覆盖了 decision.moves。
        # 没有任何一个人两端都有 node_id 时，退回「当前场景」自由文本比较
        # （兼容尚未产出 node id 的模组/历史房间）。
        after_state = await self._read_keeper_state(room_id)
        before_nodes = {pid: location_of(keeper_state, pid) for pid in turn_player_ids}
        after_nodes = {pid: location_of(after_state, pid) for pid in turn_player_ids}
        has_node_ids = any(
            before_nodes[pid] is not None and after_nodes[pid] is not None
            for pid in turn_player_ids
        )
        if has_node_ids:
            scene_changed = any(before_nodes[pid] != after_nodes[pid] for pid in turn_player_ids)
        else:
            prev_scene = (keeper_state or {}).get("当前场景")
            new_scene = (after_state or {}).get("当前场景")
            scene_changed = (
                prev_scene is not None and new_scene is not None and prev_scene != new_scene
            )
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
            checks=[c.skill_id for c in decision.checks],
            san_checks=len(decision.san_checks),
            state_updates=[u.key for u in decision.state_updates],
            moves=[f"{m.player}→{m.node_id}" for m in decision.moves],
            agenda_fired=decision.agenda_fired,
            visibility_revealed=decision.visibility_revealed,
            opening_complete=decision.opening_complete,
            ending_reached=decision.ending_reached,
            # 已经垂直切出去的能力自带审计字段（exec/27 阶段 2）——否则每加一片
            # 能力都得回来改这行，而漏了不报错、只是那片能力在日志里隐身。
            **audit_fields(decision),
            is_heartbeat=is_heartbeat,
            is_opening_ceremony=is_opening_ceremony,
            player_confused=confused,
            clear_action_intent=action_intent,
            weird_or_meta=weird,
            scene_transition=scene_changed,
        )

        # exec/19 #44：动手了，但本轮一个检定都没裁出来（裁决器没发，或护栏
        # 拦掉）。此时叙事绝不能替玩家写出攻击的成败。
        unresolved_conflict = physical_conflict and not pending_checks
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
            check_boundary_hint = _build_check_boundary_hint(pending_checks)
            narration, segments = await self._narrate_per_audience(
                room_id=room_id,
                situation=situation,
                build_situation=build_situation,
                utterances=context.utterances,
                fallback_nickname=context.player_nickname,
                fallback_utterance=context.utterance,
                decision=decision,
                report=report,
                issues=issues,
                token_limit=token_limit,
                char_limit=char_limit,
                extra_suffix=check_boundary_hint,
                action_intent=action_intent,
                confused=confused,
                keeper_state=after_state,
                players=players,
                turn_player_ids=turn_player_ids,
                private_player_ids=frozenset(context.private_player_ids),
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
        no_check_suffix = (
            _UNRESOLVED_CONFLICT_HINT if unresolved_conflict else _NO_PENDING_CHECK_HINT
        )
        narration, segments = await self._narrate_per_audience(
            room_id=room_id,
            situation=situation,
            build_situation=build_situation,
            utterances=context.utterances,
            fallback_nickname=context.player_nickname,
            fallback_utterance=context.utterance,
            decision=decision,
            report=report,
            issues=issues,
            token_limit=token_limit,
            char_limit=char_limit,
            extra_suffix=no_check_suffix,
            action_intent=action_intent,
            confused=confused,
            keeper_state=after_state,
            players=players,
            turn_player_ids=turn_player_ids,
            private_player_ids=frozenset(context.private_player_ids),
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
            rng=self._rng,
        )
        if pending.kind == "skill":
            assert pending.skill is not None
            _text, detail = await roll_check_detail(
                deps,
                pending.skill,
                pending.player_nickname,
                opposed_opponent=pending.opposed_opponent,
                opposed_value=pending.opposed_value,
            )
            notice = CheckResultNotice(
                check_request_id=pending.check_request_id,
                kind="skill",
                player_id=detail["player_id"],
                skill=detail["skill"],
                rolled=detail["rolled"],
                target=detail["target"],
                level=detail["level"],
                opposed_opponent=detail.get("opposed_opponent"),
                opposed_rolled=detail.get("opposed_rolled"),
                opposed_target=detail.get("opposed_target"),
                opposed_level=detail.get("opposed_level"),
                opposed_won=detail.get("opposed_won"),
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

    async def _record_decision(
        self,
        *,
        room_id: str,
        player_id: str,
        decision: KeeperDecision,
        forced: list[str],
    ) -> None:
        """把这一轮的裁决分类与理由写进 events（exec/25 #61）。

        为什么要有：诊断 exec/25 #59 时拿不到那一轮 `player_state` 的实际值——
        `keeper.state`/`keeper.node`/`narration.push` 都落了表，唯独**裁决本身
        没有**，而它才是"叙事为什么这么写"的唯一解释。只能靠复现探针推断，而
        探针复现的是新的一次调用，不是当时那次。

        🔴 **不落 `narration_guidance` 的内容，只落哪几条代码强制命中了。**
        guidance 里有"须保密什么"，而 `get_replay` 是把 `payload` 原样返回给
        玩家的。虽然 replay 已经显式排除了本事件类型，但不把敏感内容写进去是
        更靠前的一道——纵深防御，同「保密靠拿不到，不是请你别说」。
        `thinking` 同理是审计字段（prompt 里明写玩家看不到），它落表**只**因为
        replay 那条排除；两道都在。

        任何失败都不能连累这一轮：留痕挂了，游戏照常进行。
        """
        try:
            async with self._session_factory() as db:
                db.add(
                    Event(
                        room_id=room_id,
                        player_id=player_id,
                        event_type="keeper.decision",
                        payload={
                            "player_state": decision.player_state,
                            "thinking": decision.thinking,
                            "forced": forced,
                            "check_skill_ids": [c.skill_id for c in decision.checks],
                            "san_check_count": len(decision.san_checks),
                            "current_node_id": decision.current_node_id,
                            "ending_reached": decision.ending_reached,
                        },
                    )
                )
                await db.commit()
        except Exception as exc:  # noqa: BLE001 — 留痕失败不该让真人的这一轮失败
            logger.warning("keeper_decision_record_failed", room_id=room_id, error=str(exc))

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

    async def _narrate_per_audience(
        self,
        *,
        room_id: str,
        situation: str,
        build_situation: Callable[..., str],
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

        每一段叙事的局面块都由 `build_situation` **按这一段的受众重建**：
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
            return _build_bystander_hint(
                [
                    nicknames[pid]
                    for pid in audience
                    if pid not in turn_player_ids and pid in nicknames
                ]
            )

        if len(groups) <= 1 and not covert_speakers:
            narration = await self._narrate_prose(
                situation,
                decision,
                report,
                issues,
                max_tokens=token_limit,
                max_chars=char_limit,
                extra_suffix=extra_suffix + _bystanders(tuple(all_ids)),
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
            scoped_situation = build_situation(
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
            response = await self._client.chat.completions.create(
                tape_kind="chapter",
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": CHAPTER_SUMMARY_INSTRUCTIONS},
                    {"role": "user", "content": format_chapter_input(history_lines)},
                ],
                temperature=0.2,
                extra_body=_DISABLE_THINKING,
            )
            text = (response.choices[0].message.content or "").strip()
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
