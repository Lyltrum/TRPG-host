"""分段摘要 L2（exec/14 P4.2）—— 长战役记忆的中间层。

## 三层记忆里的位置

| 层 | 内容 | 谁写 | 活多久 |
|---|---|---|---|
| L1 事实账本（`fact_ledger`） | 已确认的线索 | **代码** | 永久 |
| **L2 分段摘要（本模块）** | 一段剧情的梗概 | LLM，**离线** | 永久 |
| L3 历史窗口（`_load_room_memory`） | 最近 200 条事件原文 | —— | 滑动窗口 |

L1 保证"事实不丢"，但一场戏里还有大量**不是线索**却影响后续的东西：跟谁翻过
脸、许过什么诺、哪扇门被撞坏了。这些逐条记账不现实（判据不清晰），摘要正好
承担——**"必须记住"的走 L1，"记得大概就行"的走 L2**。

## 为什么存 events 表

跟 L1 同一个理由：只追加、带时间戳、按 room 索引。`room_summaries` 表不能
复用——它 `room_id` 唯一（一房一条），是复盘用的终局总结，不是分段的。

## 触发：场景切换 + 至少 N 轮

只按场景切换会在玩家来回踱步时疯狂触发；只按轮数会把一段完整的戏拦腰截断。
两者取交集：**换了场景，且距上次摘要已经积累了足够多轮**。

## 离线：不在玩家等待路径上

摘要生成是后台任务（`asyncio.create_task`），失败只记日志不影响这一轮回应。
玩家等的是叙事，不该为"整理笔记"多等几秒。
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event

logger = structlog.get_logger()

EVENT_TYPE = "keeper.chapter"

#: 两次摘要之间至少要积累这么多轮，才允许在场景切换时再摘一次。
MIN_TURNS_BETWEEN_CHAPTERS = 12

#: 一段摘要的字数上限——它要长期常驻上下文，不能自己变成新的上下文负担。
CHAPTER_MAX_CHARS = 120


def should_summarize(*, scene_changed: bool, turns_since_last: int) -> bool:
    """纯函数，方便单测与调参。见模块 docstring 的取交集理由。"""
    return scene_changed and turns_since_last >= MIN_TURNS_BETWEEN_CHAPTERS


async def turns_since_last_chapter(db: AsyncSession, *, room_id: str) -> int:
    """上次摘要之后又发生了多少轮玩家行动。"""
    last = await db.execute(
        select(Event.created_at)
        .where(Event.room_id == room_id, Event.event_type == EVENT_TYPE)
        .order_by(Event.created_at.desc())
        .limit(1)
    )
    since = last.scalar_one_or_none()
    stmt = select(Event.id).where(Event.room_id == room_id, Event.event_type == "action.submit")
    if since is not None:
        stmt = stmt.where(Event.created_at > since)
    rows = await db.execute(stmt)
    return len(rows.all())


async def record_chapter(db: AsyncSession, *, room_id: str, text: str) -> None:
    """落一段摘要。调用方负责 commit。"""
    clean = text.strip()
    if not clean:
        return
    db.add(
        Event(
            room_id=room_id,
            player_id=None,
            event_type=EVENT_TYPE,
            payload={"text": clean[:CHAPTER_MAX_CHARS]},
        )
    )
    logger.info("keeper_chapter_recorded", room_id=room_id, length=len(clean))


async def load_chapters(db: AsyncSession, *, room_id: str) -> list[str]:
    """全部摘要，按发生顺序。**不设 limit**——与 L1 同理，必须活过 L3 的窗口。"""
    rows = await db.execute(
        select(Event.payload)
        .where(Event.room_id == room_id, Event.event_type == EVENT_TYPE)
        .order_by(Event.created_at, Event.id)
    )
    return [str((p or {}).get("text", "")).strip() for (p,) in rows if (p or {}).get("text")]


def render_chapters(chapters: list[str]) -> str:
    """渲染成注入局面块的「前情提要」。空列表返回空串，调用方整块省略。"""
    if not chapters:
        return ""
    return "\n".join(f"{i}. {text}" for i, text in enumerate(chapters, start=1))


def build_recap(chapters: list[str], ledger_text: str) -> str:
    """上集回顾：给**玩家**看的跨会话回顾（L2 梗概 + L1 已确认线索）。

    ⚠️ 内容函数，还没有投递通道——"在哪个界面、什么时机给玩家看"是产品决定，
    且天然属于 P5（per-observer 投递）的范围。这里先把内容备好。
    """
    parts: list[str] = []
    if chapters:
        parts.append("上次的进展：\n" + render_chapters(chapters))
    if ledger_text:
        parts.append("你们已经确认的线索：\n" + ledger_text)
    return "\n\n".join(parts)
