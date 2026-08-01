"""AI 玩家的回合参与（exec/21 第三层的服务层）。

职责只有一条：**在真人这一轮里，问问桌上的 AI 队友要不要跟一句**。

它不决定叙事、不掷骰、不改状态——决定出来的那句话由 ws.py 走跟真人**完全
相同**的 `action.submit` 路径提交（落库 → 广播原话 → 并入本轮收集窗口）。

## 时机：跟随，不抢先

调用点在收集窗口**关闭之后、裁决之前**：真人先说完，AI 才补。反过来（AI 先
开口、真人跟着它走）会让桌子变成"看 AI 演"——它是补位的，不是主角。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai_actor import AiActor, build_view
from app.core.keeper.history import (
    HISTORY_EVENT_TYPES,
    HISTORY_LIMIT,
    history_lines_from_events,
)
from app.models.event import Event
from app.models.room import Character, Player

logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class AiSubmission:
    """一个 AI 队友这一轮要说的话。"""

    player_id: str
    nickname: str
    utterance: str


async def collect_ai_submissions(
    db: AsyncSession, room_id: str, actor: AiActor | None
) -> list[AiSubmission]:
    """问遍房里的 AI 队友，收回它们这一轮要说的话（可能一句都没有）。

    - `actor is None`（没配 key）或房里没有 AI → 直接空列表，一次查询都不多做；
    - 每个 AI 各一次 LLM 调用，**并发**：两个 AI 队友不该让玩家等两倍时间；
    - 任何一个失败 → 那一个沉默（`AiActor.decide` 内部已兜底），不影响别人。
    """
    if actor is None:
        return []
    ai_players = list(
        (
            await db.execute(
                select(Player).where(Player.room_id == room_id, Player.is_ai.is_(True))
            )
        ).scalars()
    )
    if not ai_players:
        return []

    characters = list(
        (await db.execute(select(Character).where(Character.room_id == room_id))).scalars()
    )
    chars_by_player = {c.player_id: c for c in characters}
    all_players = list(
        (await db.execute(select(Player).where(Player.room_id == room_id))).scalars()
    )
    nicknames = {p.id: p.nickname for p in all_players}

    events = list(
        (
            await db.execute(
                select(Event)
                .where(Event.room_id == room_id, Event.event_type.in_(HISTORY_EVENT_TYPES))
                .order_by(Event.created_at.desc(), Event.id.desc())
                .limit(HISTORY_LIMIT)
            )
        ).scalars()
    )
    events.reverse()
    history_lines = history_lines_from_events(events, nicknames)

    async def _one(player: Player) -> AiSubmission | None:
        character = chars_by_player.get(player.id)
        if character is None:
            # AI 队友按设计一定有卡（第二层落库时一起建的）。没有 = 数据不一致，
            # 留痕但不抛：这一轮它沉默，真人的回合照常跑完。
            logger.warning("ai_player_without_character", room_id=room_id, player_id=player.id)
            return None
        view = build_view(
            character=character,
            history_lines=history_lines,
            player_id=player.id,
            # 名单里去掉它自己：这是"同桌还有谁"，不是花名册
            roster=[n for pid, n in nicknames.items() if pid != player.id],
        )
        intent = await actor.decide(view)
        logger.info(
            "ai_player_intent",
            room_id=room_id,
            player_id=player.id,
            act=intent.act,
            thinking=intent.thinking,
        )
        if not intent.act or not intent.utterance:
            return None
        return AiSubmission(
            player_id=player.id, nickname=player.nickname, utterance=intent.utterance
        )

    results = await asyncio.gather(*(_one(p) for p in ai_players))
    return [r for r in results if r is not None]
