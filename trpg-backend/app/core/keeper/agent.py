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
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.keeper.decision import (
    KeeperDecision,
    create_pending_checks,
    execute_side_effects,
)
from app.core.keeper.module_loader import ScenarioModule
from app.core.keeper.pending import PendingCheck, pending_check_manager
from app.core.keeper.prompts import (
    build_adjudicator_instructions,
    build_narrator_instructions,
    format_narrator_input,
    format_turn_input,
)
from app.core.keeper.tools import KeeperDeps, KeeperToolError, roll_check_detail, san_check_detail
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

# 全量重放 events 的上限条数。短模组一场 2-3 小时也就几百条，全放得下
# （DeepSeek 64K 上下文）；上限只是防御异常膨胀的房间。
_HISTORY_LIMIT = 200

# 裁决 JSON 解析失败时的重试次数（把解析错误喂回去让模型改）。
_ADJUDICATE_RETRIES = 1

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
        self._client = AsyncOpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
        self._adjudicator_instructions = build_adjudicator_instructions(module)
        self._narrator_instructions = build_narrator_instructions(module)

    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        if context.room_id is None or context.player_id is None:
            raise ValueError("KeeperAgent 需要 NarrationContext 携带 room_id/player_id")
        room_id = context.room_id

        # 两段式玩家掷骰：还有待掷的检定时不再裁决新一轮——先让玩家把手头的
        # 骰子掷完。重发同一个请求（而不是静默不回应），防前端刷新丢卡片。
        pending = pending_check_manager.first(room_id)
        if pending is not None:
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
        situation = format_turn_input(
            keeper_state, history_lines, roster, context.player_nickname, context.utterance
        )

        # 阶段1·裁决：结构化输出，检定是 schema 字段，不存在"忘了裁决"。
        decision = await self._adjudicate(situation)
        logger.info(
            "keeper_decision",
            thinking=decision.thinking,
            checks=[c.skill for c in decision.checks],
            san_checks=len(decision.san_checks),
            hp_changes=len(decision.hp_changes),
            state_updates=[u.key for u in decision.state_updates],
        )

        # 阶段2·执行：HP/状态纯代码立即写库；检定不在这里掷骰，解析成待掷记录。
        deps = KeeperDeps(
            room_id=room_id,
            player_id=context.player_id,
            session_factory=self._session_factory,
            module=self._module,
            ruleset=self._ruleset,
            rng=self._rng,
        )
        report, issues = await execute_side_effects(deps, decision)
        pending_checks, pending_issues = await create_pending_checks(deps, decision)
        issues = [*issues, *pending_issues]

        if pending_checks:
            pending_check_manager.add(room_id, pending_checks)
            check_list = "\n".join(
                f"- {c.player_nickname} · {'理智' if c.kind == 'san' else c.skill}检定"
                f"（{c.reason or '无说明'}）"
                for c in pending_checks
            )
            guidance = (
                f"{decision.narration_guidance}\n\n"
                "## 本轮已发起的检定请求（叙事写到需要掷骰为止，渲染紧张时刻，"
                "由情境示意玩家掷骰；绝不编造检定结果，也**不要提前描写任何"
                "要靠这次检定才能获得的信息**——线索一个字都留到掷骰之后）\n" + check_list
            )
            decision_for_narration = decision.model_copy(update={"narration_guidance": guidance})
            narration = await self._narrate_prose(situation, decision_for_narration, report, issues)
            return NarrationOutcome(
                text=narration,
                check_requests=[_pending_to_notice(c) for c in pending_checks],
            )

        # 阶段3·叙事：只写故事。
        narration = await self._narrate_prose(situation, decision, report, issues)

        # 🔴 掷骰可见性的硬保证：本轮发生过的伤害由代码强制附加在叙事末尾——
        # 骰子/数值当众认账是机制不是要求（实测模型会把数字藏进叙事）。
        if deps.check_results:
            dice_lines = "\n".join(f"🎲 {line}" for line in deps.check_results)
            narration = f"{narration}\n\n{dice_lines}" if narration else dice_lines
        return NarrationOutcome(text=narration)

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
        """阶段1：裁决。JSON mode + pydantic 校验，解析失败把错误喂回去重试一次。

        温度压低（0.3）：裁决要的是稳定一致的规则判断，不是创造力。
        """
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": self._adjudicator_instructions},
            {"role": "user", "content": situation + "\n\n请输出本轮的裁决 JSON。"},
        ]
        last_error: Exception | None = None
        for _ in range(1 + _ADJUDICATE_RETRIES):
            response = await self._client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.3,
            )
            raw = response.choices[0].message.content or ""
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
        raise RuntimeError(f"裁决 JSON 解析失败：{last_error}")

    async def _narrate_prose(
        self,
        situation: str,
        decision: KeeperDecision,
        report: list[str],
        issues: list[str],
    ) -> str:
        """阶段3：叙事。没有工具、没有裁决压力，写作本能是生产力不是对抗对象。"""
        user_content = format_narrator_input(situation, decision.narration_guidance, report, issues)
        response = await self._client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": self._narrator_instructions},
                {"role": "user", "content": user_content},
            ],
            temperature=0.8,
        )
        return response.choices[0].message.content or ""

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
                lines.append(f"守秘人：{payload.get('text', '')}")
            elif event.event_type == "keeper.check":
                lines.append(
                    f"[检定] {payload.get('player', '')} {payload.get('skill', '')}："
                    f"{payload.get('rolled', '?')}/{payload.get('target', '?')} "
                    f"→ {payload.get('level', '')}"
                )
            elif event.event_type == "keeper.san":
                lines.append(
                    f"[理智] {payload.get('player', '')}：损失 {payload.get('loss', '?')}，"
                    f"当前 San {payload.get('san', '?')}"
                )
            elif event.event_type == "keeper.hp":
                lines.append(
                    f"[生命] {payload.get('player', '')}：{payload.get('delta', '?')}"
                    f"（{payload.get('reason', '')}），当前 HP {payload.get('hp', '?')}"
                )
        return keeper_state, lines, roster
