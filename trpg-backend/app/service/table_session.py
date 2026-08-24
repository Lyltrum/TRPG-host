"""聚会场次：一局跑几个晚上（`exec/46` B3）。

## 它解决的不是「战役」

用户 2026-08-24 定的形状：**不做角色带到下一场**。角色不带走，通常意味着
角色压根没离开过——同一批人、同一批角色、同一个故事，只是**一个晚上跑不完，
下周接着跑**。这在桌游里不叫战役，叫分次跑完一个模组。

所以这里**没有** campaigns 表、没有跨房间的角色、没有成长检定（成长是局末
结算，而这局根本没结束）。

## 跟「先休息一下」是两档粒度，不是同一个开关

`room.paused`（`exec/35`）是几分钟：上厕所、点外卖。**任何玩家都能按**，
按下去什么都不生成。

这里的散会是几天：今晚打完收工，下周接着。**只有房主能按**，按下去要留下
「上次讲到哪」。

两者共用的只有一件事——**都不开新的一轮**。那件事收在 `table_is_open()` 里，
🔴 **加第三种停时只改那一处**（项目判据：逐个列出的断言，加一项就漏一项）。

## 为什么散会用 `room.phase` 而不是再加一个 bool

`paused` 已经有主人了（临时休息），蹭它就是「一份数据扮演两个角色」。而散会
本来就是**大厅级的生命周期状态**——它跟 Lobby/Building/InGame/Completed 是
同一个维度上的东西：这一场聚会开着没有。

🔴 命名用 `Adjourned`（散会）不用 `Paused`：跟 `room.paused` 撞名会让下一个人
（包括下一轮的我）以为它们是一回事。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.table_state import SESSION_ACTIVE, SESSION_ENDED
from app.models.replay import RoomSession


async def current_session(db: AsyncSession, room_id: str) -> RoomSession | None:
    """这个房间正开着的那一场聚会，没有就是 None。

    按 `started_at` 倒序而不是 `created_at`：两列在正常路径上同时写，但排序
    依据该是「哪一场先开始」这件事本身。
    """
    return await db.scalar(
        select(RoomSession)
        .where(RoomSession.room_id == room_id, RoomSession.status == SESSION_ACTIVE)
        .order_by(RoomSession.started_at.desc())
    )


async def open_session(db: AsyncSession, room_id: str) -> RoomSession:
    """开一场新的聚会。正式开局与每次续跑各调一次。

    **不 commit**：调用方那一笔事务里还有别的写（开局要改 phase、续跑要清
    散会标记），分两次提交会留下「场次开了但房间还是散会状态」的中间态。
    """
    session = RoomSession(
        room_id=room_id,
        status=SESSION_ACTIVE,
        started_at=datetime.now(UTC),
    )
    db.add(session)
    return session


async def close_session(db: AsyncSession, room_id: str) -> RoomSession | None:
    """结掉正开着的那一场。没有开着的场次返回 None——**不新建一行**。

    老房间（这条线上线之前开的局）一行场次记录都没有，散会时不该凭空补一个
    起点不明的场次出来。
    """
    session = await current_session(db, room_id)
    if session is None:
        return None
    session.status = SESSION_ENDED
    session.ended_at = datetime.now(UTC)
    return session


async def session_count(db: AsyncSession, room_id: str) -> int:
    """这一局到今天为止聚过几次。前端拿它显示「第 3 次聚会」。"""
    rows = await db.scalars(select(RoomSession).where(RoomSession.room_id == room_id))
    return len(list(rows))
