"""KeeperAgent v2：两阶段回合制的 COC 守秘人（裁决与叙事分离）。

对外它仍只是一个 `Narrator`——WS 层照常 `narrate(context) -> str`，协议/锁/
广播零改动；改造全部发生在 narrate() 内部。

v1（openai-agents 自由工具调用）为什么被推翻：一次 LLM 调用同时承担
理解/裁决/记账/叙事，模型的写作本能碾压其余三件，实测四类 bug 同一病灶
（该掷不掷、线索白给、状态不记、骰值藏进叙事），三轮 prompt 强化 + 两次
结构强制都只是补丁。v2 仿真人 KP 的台前/幕后分离：

    action.submit
      ↓ 阶段1·裁决（LLM，JSON mode，低温）→ KeeperDecision
      ↓ 阶段2·执行（纯代码）           → 服务端掷骰/写库/留痕
      ↓ 阶段3·叙事（LLM，只写故事）    → 广播文本
      ↓ 代码强制附加 🎲 检定行（可见性硬保证，不依赖模型自觉）

openai-agents SDK 不再出现在这条主路径上（依赖暂保留，未来多 agent 实验
可能复用）。
"""

import random

import structlog
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.keeper.decision import KeeperDecision, execute_decision
from app.core.keeper.module_loader import ScenarioModule
from app.core.keeper.prompts import (
    build_adjudicator_instructions,
    build_narrator_instructions,
    format_narrator_input,
    format_turn_input,
)
from app.core.keeper.tools import KeeperDeps
from app.core.narrator import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    NarrationContext,
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

    async def narrate(self, context: NarrationContext) -> str:
        if context.room_id is None or context.player_id is None:
            raise ValueError("KeeperAgent 需要 NarrationContext 携带 room_id/player_id")

        keeper_state, history_lines, roster = await self._load_room_memory(context.room_id)
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

        # 阶段2·执行：纯代码掷骰/写库，LLM 摸不到骰子。
        deps = KeeperDeps(
            room_id=context.room_id,
            player_id=context.player_id,
            session_factory=self._session_factory,
            module=self._module,
            ruleset=self._ruleset,
            rng=self._rng,
        )
        report, issues = await execute_decision(deps, decision)

        # 阶段3·叙事：只写故事。
        narration = await self._narrate_prose(situation, decision, report, issues)

        # 🔴 掷骰可见性的硬保证：本轮发生过的检定/理智/伤害由代码强制附加在
        # 叙事末尾——骰子当众认账是机制不是要求（实测模型会把数字藏进叙事）。
        if deps.check_results:
            dice_lines = "\n".join(f"🎲 {line}" for line in deps.check_results)
            narration = f"{narration}\n\n{dice_lines}" if narration else dice_lines
        return narration

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
