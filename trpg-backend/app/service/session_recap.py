"""「上次讲到哪」：续跑时的开场白（`exec/46` B3）。

## 跟局末复盘不是一回事

`recap.py` 是**散场后的回顾**：这一局玩完了，受众是已经知道结果的人，
`finished` 之后还会附上没揭开的谜底。

这里是**开场白**：这一局还在跑，下周大家重新围到桌边，需要一段「上次打到
哪」把人接回去。两者的受众、时机、能说的话都不同——最关键的一条是**它不能
剧透**：局还没完，谜底一个字都不能带。

所以它不复用 `build_summary`，只复用 `story_lines`（把事件流转成"谁说了
什么"）。那一份是纯函数，两边都需要，抽不抽都一样。

## 只喂这一场的事件

窗口是 `[session.started_at, session.ended_at]`。**不是整局**——续跑要接的是
上次那个晚上，把十个晚上的事都倒一遍就成了流水账，而它的用途恰恰是"我们上次
停在哪"。

## 懒生成

散会那一刻不算：那是一次 LLM 往返，会让「今晚到此为止」卡住十几秒，而那时
大家正在收桌子。第一次打开时算一次、落在 `RoomSession.recap_text` 上。
同 `recap` 的判据。

## 没有 key 就没有这一段，不编

跟 `recap` 一样如实降级。编一段假的"上次你们大有斩获"比没有更糟——它会被人
当成真的接着往下玩。
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.llm_tape import build_llm_client
from app.core.narration.deepseek import deepseek_base_url, deepseek_model
from app.core.table_state import SESSION_ENDED
from app.models.event import Event
from app.models.replay import RoomSession
from app.service.recap import story_lines

logger = structlog.get_logger()

_SYSTEM_PROMPT = (
    "你是一场《克苏鲁的呼唤》的守秘人。这一局还没结束，玩家们上次打到一半就散场了，"
    "下次聚会你要用一段话把他们接回来。"
    "根据下面这一场真实发生过的事件流，写 150 字以内的「上次讲到哪」："
    "他们上次做了什么、停在什么地方、手头还悬着什么没弄清楚。"
    "🔴 这一局还在跑，**绝对不要剧透**：不要暗示真相是什么、不要评价谁的判断对不对、"
    "不要提示接下来该去哪——那是他们自己要决定的事。"
    "🔴 不要编造事件流里没有的事，不要写数字。"
    "用「上次我们说到」这样的口吻，像牌桌上重新开场时随口带一句。"
)


def _timeout_seconds() -> float:
    return get_settings().recap_timeout_seconds


async def last_ended_session(db: AsyncSession, room_id: str) -> RoomSession | None:
    """最近结束的那一场聚会。一次都没散过会就是 None。"""
    return await db.scalar(
        select(RoomSession)
        .where(RoomSession.room_id == room_id, RoomSession.status == SESSION_ENDED)
        .order_by(RoomSession.ended_at.desc())
    )


async def _session_events(db: AsyncSession, session: RoomSession) -> list[Event]:
    """这一场聚会窗口内的事件。

    🔴 起点缺失就返回空，**不退化成"取全部"**：那会把十个晚上的事当成一个
    晚上讲。宁可没有这一段（如实降级），也不要一段错的。
    """
    if session.started_at is None:
        return []
    query = select(Event).where(
        Event.room_id == session.room_id, Event.created_at >= session.started_at
    )
    if session.ended_at is not None:
        query = query.where(Event.created_at <= session.ended_at)
    return list(await db.scalars(query.order_by(Event.created_at, Event.id)))


async def _write(story: list[str]) -> str | None:
    api_key = get_settings().deepseek_api_key
    if not api_key or not story:
        return None
    client = build_llm_client(
        api_key=api_key, base_url=deepseek_base_url(), timeout=_timeout_seconds()
    )
    try:
        response = await client.chat.completions.create(
            tape_kind="session_recap",
            model=deepseek_model(),
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(story)},
            ],
            extra_body={"thinking": {"type": "disabled"}},
        )
    except Exception:
        logger.warning("session_recap_llm_failed", exc_info=True)
        return None
    return (response.choices[0].message.content or "").strip() or None


async def build_session_recap(db: AsyncSession, room_id: str) -> str | None:
    """「上次讲到哪」。生成过就直接读，没有就现算一次并落库。

    返回 `None` 的三种情况都如实：还没散过会 / 那一场什么都没发生 / 没配 key。
    调用方**不要**拿一句占位文案填上去。
    """
    session = await last_ended_session(db, room_id)
    if session is None:
        return None
    if session.recap_text:
        return session.recap_text

    events = await _session_events(db, session)
    text = await _write(story_lines(events))
    if text:
        session.recap_text = text
        await db.commit()
    return text
