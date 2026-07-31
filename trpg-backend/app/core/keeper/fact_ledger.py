"""线索账本 L1（exec/14 P4）—— 长战役记忆的事实层。

## 它解决的问题

`_load_room_memory` 的 docstring 写着"重放完整历史……玩家在第 3 轮说过的话
第 30 轮还得作数"，但实现是 `.order_by(created_at.desc()).limit(200)`——
**取最近 200 条，超出后静默丢掉开头**。每轮产生 2–4 条事件，200 条约等于
最近 50–80 轮（1–2 小时）。一场几十小时的战役有上千轮，窗口只覆盖个位数
百分比，而且丢的正是委托怎么下的、最早的证词这些最需要记住的部分。

分层记忆的第一层就是回答这个：**凡是"必须记住"的，用代码记账，不放进会
滑出窗口的历史里，也不指望 LLM 自觉写进 keeper_state。**

## 存哪：`events` 表，不是 keeper_state

`events` 本来就是只追加、带时间戳、按 room 索引的——正是账本要的形状。
相比之下 `keeper_state` 是个会被整体覆写的 JSON blob，把账本塞进去既要
自己处理并发覆写，又丢掉了"何时、经由什么"这些出处信息。

一条账目 = 一个 `keeper.fact_revealed` 事件，payload 记
`{fact_id, via, detail}`，`player_id` 与 `created_at` 由事件表本身承载。
**出处（谁/何时/经由什么）是 P5 per-observer 视图的原料**，现在就记全，
免得那时再回头补数据。
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.keeper.module_loader import ScenarioModule
from app.models.event import Event

logger = structlog.get_logger()

EVENT_TYPE = "keeper.fact_revealed"


@dataclass(frozen=True)
class Revelation:
    """一条账目：某人在某时经由某种方式得到了某条事实。"""

    fact_id: str
    player_id: str | None
    via: str  # check | node | keeper
    detail: str = ""


async def record_revelations(
    db: AsyncSession,
    *,
    room_id: str,
    player_id: str | None,
    fact_ids: list[str],
    via: str,
    detail: str = "",
) -> list[str]:
    """记账。已经记过的事实不重复记（同一条线索多路径拿到只算一次）。

    调用方负责 commit——记账要和触发它的那次结算在同一个事务里，
    不能出现"检定成功了但账没记上"。
    """
    if not fact_ids:
        return []
    already = await revealed_fact_ids(db, room_id=room_id)
    fresh = [f for f in fact_ids if f not in already]
    for fact_id in fresh:
        db.add(
            Event(
                room_id=room_id,
                player_id=player_id,
                event_type=EVENT_TYPE,
                payload={"fact_id": fact_id, "via": via, "detail": detail},
            )
        )
    if fresh:
        logger.info("keeper_facts_revealed", room_id=room_id, facts=fresh, via=via)
    return fresh


async def revealed_fact_ids(db: AsyncSession, *, room_id: str) -> set[str]:
    """房间内已揭开的全部事实 id。

    **不设 limit** —— 这正是账本存在的理由：它必须活过 `_HISTORY_LIMIT`
    那个 200 条的滑动窗口。
    """
    rows = await db.execute(
        select(Event.payload).where(Event.room_id == room_id, Event.event_type == EVENT_TYPE)
    )
    out: set[str] = set()
    for (payload,) in rows:
        fact_id = (payload or {}).get("fact_id")
        if fact_id:
            out.add(str(fact_id))
    return out


async def revelations(db: AsyncSession, *, room_id: str) -> list[Revelation]:
    """带出处的完整账目，按发生顺序。P5 的 per-observer 视图要用它。"""
    rows = await db.execute(
        select(Event)
        .where(Event.room_id == room_id, Event.event_type == EVENT_TYPE)
        .order_by(Event.created_at, Event.id)
    )
    return [
        Revelation(
            fact_id=str((e.payload or {}).get("fact_id", "")),
            player_id=e.player_id,
            via=str((e.payload or {}).get("via", "")),
            detail=str((e.payload or {}).get("detail", "")),
        )
        for e in rows.scalars()
        if (e.payload or {}).get("fact_id")
    ]


def render_ledger(module: ScenarioModule, fact_ids: set[str]) -> str:
    """把账本渲染成注入裁决/叙事上下文的一段文本。

    只渲染 `diegetic` 事实：元层永远不该出现在这里（它也不可能被"揭开"）。
    空账本返回空串，调用方整块省略——短模组开局时输出不该变脏。
    """
    if not fact_ids:
        return ""
    lines = [
        f"- {f.text}"
        for f in module.facts
        if f.id in fact_ids and f.tier == "diegetic" and f.text.strip()
    ]
    return "\n".join(lines)
