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

from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.keeper.memory.history import HistoryLine, is_visible_to, visible_history
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


async def record_chapter(
    db: AsyncSession, *, room_id: str, text: str, audience: frozenset[str] | None = None
) -> None:
    """落一段摘要。调用方负责 commit。

    `audience=None` = 公开段，全房间都经历过。分头期间那几组各有自己的一段，
    受众就是那组人（与历史行的 `audience` 同一口径）。
    """
    clean = text.strip()
    if not clean:
        return
    payload: dict = {"text": clean[:CHAPTER_MAX_CHARS]}
    if audience:
        payload["audience"] = sorted(audience)
    db.add(
        Event(
            room_id=room_id,
            player_id=None,
            event_type=EVENT_TYPE,
            payload=payload,
        )
    )
    logger.info(
        "keeper_chapter_recorded",
        room_id=room_id,
        length=len(clean),
        audience=len(audience) if audience else 0,
    )


@dataclass(frozen=True, slots=True)
class Chapter:
    """一段摘要 + 它的受众。形状与 `HistoryLine` 刻意一致——同一件事。"""

    text: str
    audience: frozenset[str] | None = None


async def load_chapters(db: AsyncSession, *, room_id: str) -> list[Chapter]:
    """全部摘要，按发生顺序。**不设 limit**——与 L1 同理，必须活过 L3 的窗口。

    🔴 **不在这里按受众过滤**：一次查库要供这一轮的**所有**受众用（裁决一次、
    分组叙事每组一次），过滤是 `render` 那一层的事。在查询里裁就得每组查一次库。
    """
    rows = await db.execute(
        select(Event.payload)
        .where(Event.room_id == room_id, Event.event_type == EVENT_TYPE)
        .order_by(Event.created_at, Event.id)
    )
    chapters: list[Chapter] = []
    for (payload,) in rows:
        data = payload or {}
        text = str(data.get("text", "")).strip()
        if not text:
            continue
        raw = data.get("audience")
        chapters.append(Chapter(text, frozenset(str(x) for x in raw) if raw else None))
    return chapters


def visible_chapters(chapters: list[Chapter], audience: frozenset[str] | None) -> list[str]:
    """这组人看得见的那几段（exec/14 P5.2d 的残留缺口，2026-08-11 补上）。

    判据与历史行**共用** `is_visible_to`，含"空受众只给公开段"那条特例——
    分头期间地下室那段摘要不该出现在门厅那一段的上下文里，否则前情提要就成了
    绕过按受众裁剪的旁路（而它常驻上下文，泄得比历史还久）。
    """
    if audience is not None and not audience:
        return [c.text for c in chapters if c.audience is None]
    return [c.text for c in chapters if is_visible_to(c.audience, audience)]


#: 一组人自己那段剧情至少要有这么多行，才值得单独摘一段。
#:
#: 防的是调用次数爆炸：受众集合是按"谁在场"算的，隐匿/单人分头会造出很多只有
#: 一两行的小集合，每个都摘一次等于为一句话开一次模型调用。低于门槛的那几行
#: **不会凭空消失**——它们仍在 L3 窗口里，只是不进长期摘要。
MIN_LINES_PER_CHAPTER = 3


def split_history_for_chapters(
    lines: list[HistoryLine],
    everyone: frozenset[str] = frozenset(),
) -> list[tuple[frozenset[str] | None, list[str]]]:
    """把一段历史按受众拆成「每组各摘一段」的输入。

    公开段拿公开行；每个分头受众拿**他们看得见的全部**（含公开行）——只喂私密
    行的话模型会摘出一段没有上下文的怪话。代价是公开内容在两段里重复出现，
    读起来啰嗦；**但那是文字冗余，不是泄密**，而泄密不可逆。

    ## 🔴 `everyone`：受众覆盖全场 = 它本来就是公开的（2026-08-15）

    原来的 docstring 写着「未分头时返回的就是一条公开段（退化保证）」——
    **那个保证在真实单人局里不成立**。实测 08-14 那 28 轮，每次章节摘要都
    成对出现、间隔 2–3 秒、内容近似但不完全相同：

        12:26:26 keeper.chapter（无 audience）
        12:26:29 keeper.chapter（audience=[玩家]）

    因为潜行/私密投递会给 narration 打上 audience，而单人局里那个 audience
    就是**全部在场玩家**。于是"公开一段 + 那一组一段"——两段给同一个人看。
    代价是每次摘要 2× LLM 调用，L2 记忆里还堆重复内容。

    修法不是特判"只有一个人"，而是把判据说准：**受众等于全场时它就是公开的**，
    那几行并进公开段。三人局里投递给全部三人的行同理。`everyone` 传空集就
    退化成旧行为（调用方拿不到名单时不猜）。
    """
    effective = [
        HistoryLine(text=line.text, audience=None)
        if line.audience is not None and everyone and line.audience >= everyone
        else line
        for line in lines
    ]
    public = [line.text for line in effective if line.audience is None]
    out: list[tuple[frozenset[str] | None, list[str]]] = []
    if public:
        out.append((None, public))
    seen: set[frozenset[str]] = set()
    for line in effective:
        if line.audience is None or line.audience in seen:
            continue
        seen.add(line.audience)
        private_count = sum(1 for other in effective if other.audience == line.audience)
        if private_count < MIN_LINES_PER_CHAPTER:
            continue
        out.append((line.audience, visible_history(effective, line.audience)))
    return out


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
