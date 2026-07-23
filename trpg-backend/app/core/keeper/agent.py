"""KeeperAgent：真正的 COC 守秘人（keeper agent 实验，基于 openai-agents SDK）。

对外它只是一个 `Narrator`——WS 层照常 `narrate(context) -> str`，协议/锁/
广播零改动；SDK（Agent/Runner/function_tool）是本目录内部的实现细节，
想换 pydantic-ai 或手写 loop 时只动 `keeper/`。

与 `DeepSeekNarrator`（单轮叙事替身）的本质区别：这里的 LLM 拥有工具
（掷骰/角色卡/剧本/状态/HP/San），自己决定"这句话要不要检定、查哪段剧本、
记什么状态"，是完整的守秘人职责，不再只是文案生成。
"""

import random

from agents import Agent, ModelSettings, OpenAIChatCompletionsModel, Runner, set_tracing_disabled
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.keeper.module_loader import ScenarioModule
from app.core.keeper.prompts import build_keeper_instructions, format_turn_input
from app.core.keeper.tools import KEEPER_TOOLS, KeeperDeps
from app.core.narrator import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    NarrationContext,
    Narrator,
)
from app.dto.game import RulesetRead
from app.models.event import Event
from app.models.room import Character, Player, Room

# 全量重放 events 的上限条数。短模组一场 2-3 小时也就几百条，全放得下
# （DeepSeek 64K 上下文）；上限只是防御异常膨胀的房间。
_HISTORY_LIMIT = 200

# 一轮回应允许的最大 agent loop 轮数（每轮 = 一次 LLM 调用，可能带工具调用）。
# 正常一轮行动 2-4 跳（查剧本→掷骰→生成），12 是防失控的余量。
_MAX_TURNS = 12

# 事件类型 → 历史行格式化器。keeper.state 不进历史（状态笔记单独整体注入），
# 工具留痕（检定/HP/San）进历史是为了让 agent 记得自己此前的裁决结果。
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
        # 没配 OpenAI 平台账号，trace 上传只会报错刷日志。
        set_tracing_disabled(True)
        self._module = module
        self._ruleset = ruleset
        self._session_factory = session_factory
        self._rng = rng if rng is not None else random.Random()
        client = AsyncOpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
        self._agent = Agent[KeeperDeps](
            name="守秘人",
            instructions=build_keeper_instructions(module),
            tools=KEEPER_TOOLS,
            # DeepSeek 只有 Chat Completions 接口，不能用 SDK 默认的 Responses API。
            model=OpenAIChatCompletionsModel(model=DEEPSEEK_MODEL, openai_client=client),
            # tool_choice="required"：每轮第一次推理**必须**调一个工具——要么
            # roll_check 掷骰，要么 declare_no_check 书面声明"本轮不掷"。真实
            # DeepSeek 实测三轮 prompt 强化都拽不动它的工具纪律（隐式调查动作
            # 零检定、线索白给），只能结构性强制。SDK 默认 reset_tool_choice=True，
            # 首次工具调用后自动回落 auto，不会无限循环。
            model_settings=ModelSettings(temperature=0.8, tool_choice="required"),
        )

    async def narrate(self, context: NarrationContext) -> str:
        if context.room_id is None or context.player_id is None:
            raise ValueError("KeeperAgent 需要 NarrationContext 携带 room_id/player_id")

        keeper_state, history_lines, roster = await self._load_room_memory(context.room_id)
        turn_input = format_turn_input(
            keeper_state, history_lines, roster, context.player_nickname, context.utterance
        )
        deps = KeeperDeps(
            room_id=context.room_id,
            player_id=context.player_id,
            session_factory=self._session_factory,
            module=self._module,
            ruleset=self._ruleset,
            rng=self._rng,
        )
        result = await Runner.run(self._agent, turn_input, context=deps, max_turns=_MAX_TURNS)
        narration = str(result.final_output or "")

        # 🔴 掷骰可见性的硬保证：本轮发生过的检定/理智/伤害，由代码强制附加在
        # 叙事末尾，不依赖模型"把数字写进叙事"的自觉（实测它会藏——玩家掷出
        # 94/29 失败，叙事只说"什么也没找到"，玩家以为根本没掷）。骰子当众
        # 认账是 KP 职责，职责的兜底在代码不在 prompt。
        if deps.check_results:
            dice_lines = "\n".join(f"🎲 {line}" for line in deps.check_results)
            narration = f"{narration}\n\n{dice_lines}" if narration else dice_lines
        return narration

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
