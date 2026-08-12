"""局末复盘：代码统计 + 一段模型写的回顾。

## 为什么是两半

用户拍板的形状：**上半确定性、下半有味道。**

- 数字（时长、轮数、掷骰成败、SAN 掉了多少、揭开几条线索）**一律代码算**。
  实测早就证明模型会把掷骰数字藏进散文、写错点数——凡是能确定判断的一律
  代码强制，这是项目的老判据。
- 「这一局发生了什么」交给模型写一段。数据表读起来没有"原来那个 NPC 是……"
  的味道，而复盘的价值一半在那儿。

## 🔴 没有 key 就只有上半，不伪造下半

`summary_text` 为 `None` 是**如实的降级**，不是失败：CI/e2e 不配
`DEEPSEEK_API_KEY`，那时复盘照样能开，只是没有那段回顾。编一段假的
"这一局惊心动魄"比没有更糟。

## 懒生成，不在结束那一刻算

`end_game` / `disband_room` 同步生成会让"结束游戏"卡住十几秒（一次 LLM 往返），
而复盘不一定有人看。第一次打开复盘时算一次、落库，之后直接读。
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.keeper.primitives.dice import (
    LEVEL_CRITICAL,
    LEVEL_EXTREME,
    LEVEL_FUMBLE,
    LEVEL_HARD,
    LEVEL_REGULAR,
)
from app.core.llm_tape import build_llm_client
from app.core.narration.deepseek import DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
from app.dto.replay import RoomSummaryRead
from app.models.event import Event
from app.models.replay import RoomSummary
from app.models.room import Room

logger = structlog.get_logger()

_TIMEOUT_SECONDS = 30.0

#: 算作"成功"的等级。**列出来而不是写 `!= 失败`**：大失败也不是"失败"这个
#: 字符串，用否定式会把它算成成功。
_SUCCESS_LEVELS = frozenset({LEVEL_CRITICAL, LEVEL_EXTREME, LEVEL_HARD, LEVEL_REGULAR})

_RECAP_SYSTEM_PROMPT = (
    "你是刚跑完一局《克苏鲁的呼唤》的守秘人，现在给玩家写一段复盘。"
    "根据下面这局真实发生过的事件流，写 200 字以内的回顾："
    "他们做了什么、关键的转折在哪、最后停在哪里。"
    "🔴 不要编造事件流里没有的事，不要写数字（点数、次数、剩余值都由系统另行给出），"
    "不要写「你们真棒」这类客套。用讲故事的口吻，像牌桌收摊后随口聊两句。"
)


def _format_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    return f"{minutes // 60} 小时 {minutes % 60} 分钟"


def build_highlights(room: Room, events: list[Event]) -> list[str]:
    """这一局的数字。**纯函数**，不查库、不打网络——单测直接喂事件就能验。

    只放真的发生过的项：一次检定都没掷的局不该出现"掷骰 0 次"那一行，
    那是噪音（同局面块"没有内容就整块不渲染"的退化保证）。
    """
    lines: list[str] = []

    if room.ended_at is not None and room.created_at is not None:
        elapsed = (room.ended_at - room.created_at).total_seconds()
        lines.append(f"这一局跑了 {_format_duration(elapsed)}")

    turns = sum(1 for e in events if e.event_type == "narration.push")
    if turns:
        lines.append(f"守秘人叙述了 {turns} 次")

    checks = [e for e in events if e.event_type == "keeper.check"]
    if checks:
        succeeded = sum(1 for e in checks if (e.payload or {}).get("level") in _SUCCESS_LEVELS)
        fumbles = sum(1 for e in checks if (e.payload or {}).get("level") == LEVEL_FUMBLE)
        line = f"掷了 {len(checks)} 次检定，成功 {succeeded} 次"
        if fumbles:
            line += f"（其中大失败 {fumbles} 次）"
        lines.append(line)

    san_events = [e for e in events if e.event_type == "keeper.san"]
    if san_events:
        lost = sum(int((e.payload or {}).get("loss") or 0) for e in san_events)
        lines.append(f"理智检定 {len(san_events)} 次，一共掉了 {lost} 点 SAN")

    madness = [e for e in events if e.event_type == "keeper.madness"]
    if madness:
        who = "、".join(
            dict.fromkeys(str((e.payload or {}).get("player") or "有人") for e in madness)
        )
        lines.append(f"{who} 陷入过临时性疯狂")

    damage = [e for e in events if e.event_type == "keeper.hp"]
    if damage:
        taken = sum(-int((e.payload or {}).get("delta") or 0) for e in damage)
        if taken > 0:
            lines.append(f"调查员一共挨了 {taken} 点伤")

    revealed = sum(1 for e in events if e.event_type == "keeper.fact_revealed")
    if revealed:
        lines.append(f"挣得了 {revealed} 条线索")

    return lines


def _story_lines(events: list[Event]) -> list[str]:
    """喂给模型的事件流：玩家原话 + 守秘人叙事，按时间正序。

    只喂这两类。检定明细、状态记账那些是**给系统看的**，塞进去只会让它去写
    数字——而数字那一半已经由 `build_highlights` 确定性地算好了。
    """
    lines: list[str] = []
    for event in events:
        payload = event.payload or {}
        if event.event_type == "action.submit":
            text = str(payload.get("text") or payload.get("utterance") or "").strip()
            if text:
                lines.append(f"玩家：{text}")
        elif event.event_type == "narration.push":
            text = str(payload.get("text") or "").strip()
            if text:
                lines.append(f"守秘人：{text}")
    return lines


async def _write_recap(story: list[str]) -> str | None:
    """调一次模型写回顾。没有 key ⇒ None（如实降级，不编）。"""
    api_key = get_settings().deepseek_api_key
    if not api_key or not story:
        return None
    client = build_llm_client(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=_TIMEOUT_SECONDS)
    try:
        response = await client.chat.completions.create(
            tape_kind="recap",
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": _RECAP_SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(story)},
            ],
            extra_body={"thinking": {"type": "disabled"}},
        )
    except Exception:
        # 🔴 复盘失败不该让整个页面打不开：数字那一半是确定性的、已经算好了。
        # 吞掉异常但**留痕**——静默失败会让"为什么没有回顾"永远查不出来。
        logger.warning("recap_llm_failed", exc_info=True)
        return None
    return (response.choices[0].message.content or "").strip() or None


async def build_summary(db: AsyncSession, room_id: str) -> RoomSummaryRead:
    """复盘摘要。已经生成过就直接读，没有就现算一次并落库。"""
    existing = await db.scalar(select(RoomSummary).where(RoomSummary.room_id == room_id))
    if existing is not None:
        return RoomSummaryRead(
            room_id=room_id,
            summary_text=existing.summary_text,
            highlights=existing.highlights,
        )

    room = await db.get(Room, room_id)
    if room is None:
        raise LookupError("房间不存在")
    events = list(
        await db.scalars(
            select(Event).where(Event.room_id == room_id).order_by(Event.created_at, Event.id)
        )
    )

    highlights = build_highlights(room, events)
    summary_text = await _write_recap(_story_lines(events))

    # 🔴 只在**这一局已经结束**时落库。还在跑的局也允许看复盘（中途想回顾
    # 一下很正常），但那时的统计是半截的，存下来之后就再也不会更新了。
    if room.phase == "Completed":
        db.add(RoomSummary(room_id=room_id, summary_text=summary_text, highlights=highlights))
        await db.commit()
    return RoomSummaryRead(room_id=room_id, summary_text=summary_text, highlights=highlights)
