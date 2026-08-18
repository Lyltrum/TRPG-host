"""「一拍」的边界。

一次玩家发言会引发**多次裁决**：每掷完一批骰子就有一次结算叙事，而结算叙事
本身又是一次完整裁决（见 `agent._after_check`）。所以"这一拍"不等于"这一次
执行"——凡是**每拍最多算一次**的东西，都要拿这里的判断去挡住第二次。

分界线是最后一条 `action.submit`：它之后发生的一切属于同一拍。

## 🔴 为什么下沉

它有两个用户，而且是先后长出来的：

- `san_check`：一拍之内只掷一次理智（2026-08-16 真机，三次裁决掷了三次 SAN，
  第三次当场触发一次本不该有的临时性疯狂）。
- `closure`：一拍之内只计一次「无进展轮数」（2026-08-18 真机，19 拍里那个数
  一度到 15——掷骰结算也会走一遍记账，于是**每次检定把它推高 2**）。

两处各写各的就是「同一件事有两种做法 = 那条不变式还没成立」。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event


async def happened_this_beat(db: AsyncSession, room_id: str, event_type: str) -> bool:
    """这一拍里有没有发生过 `event_type`。

    🔴 **一条玩家发言都没有时一律返回 False**，不退化成"这一局里有没有发生过"。
    那一版（2026-08-18 写出来当场被磁带测试抓到）会把整局当成一拍：磁带回放
    直接调 `take_turn`、不经过 `ws.py`，于是一条 `action.submit` 都没有，
    「无进展轮数」第一次记完之后**再也不累加**，两轮的叙事上下文全变了。

    返回 False 对两个调用方都是正确的那一侧：开场那一拍还没掷过理智（该放行），
    也还没计过停滞（该累加）。
    """
    last_utterance = (
        await db.execute(
            select(Event.created_at)
            .where(Event.room_id == room_id, Event.event_type == "action.submit")
            .order_by(Event.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if last_utterance is None:
        return False
    stmt = (
        select(Event.id)
        .where(Event.room_id == room_id, Event.event_type == event_type)
        .where(Event.created_at > last_utterance)
    )
    return (await db.execute(stmt.limit(1))).scalar_one_or_none() is not None
