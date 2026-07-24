"""世界心跳 ticker（路线第 6 步 · 提案②）。

全局单个 asyncio 任务，周期性扫描有 WS 连接的 InGame 房间；满足沉默/无
待掷/锁空闲/节流后，走同一套 narrate 管线（utterance=「时间悄然流逝」）。

默认关闭：`KEEPER_HEARTBEAT_ENABLED=false`。e2e 零感知。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.keeper.pending import pending_check_manager
from app.core.narrator import NarrationContext, Narrator
from app.models.event import Event
from app.models.room import Player, Room
from app.service.action_lock import action_lock_manager
from app.service.ws_manager import manager as ws_manager

logger = structlog.get_logger()

HEARTBEAT_UTTERANCE = "（时间悄然流逝）"

# 进程内节流（房间级）
_last_heartbeat_at: dict[str, float] = {}
_consecutive_heartbeats: dict[str, int] = {}
_last_activity_at: dict[str, float] = {}


def touch_activity(room_id: str) -> None:
    """任意玩家行动后更新活动时间，并清零连续心跳计数。"""
    _last_activity_at[room_id] = time.monotonic()
    _consecutive_heartbeats[room_id] = 0


def reset_heartbeat_state_for_tests() -> None:
    """测试夹具：清空进程内节流状态。"""
    _last_heartbeat_at.clear()
    _consecutive_heartbeats.clear()
    _last_activity_at.clear()


def _now() -> float:
    return time.monotonic()


async def _last_event_age_seconds(
    session_factory: async_sessionmaker[AsyncSession], room_id: str
) -> float | None:
    """距最后一条 action/narration/keeper 事件的秒数；无事件则 None。"""
    async with session_factory() as db:
        result = await db.execute(
            select(Event.created_at)
            .where(
                Event.room_id == room_id,
                Event.event_type.in_(
                    [
                        "action.submit",
                        "narration.push",
                        "keeper.check",
                        "keeper.san",
                        "keeper.heartbeat",
                    ]
                ),
            )
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(1)
        )
        created = result.scalar_one_or_none()
    if created is None:
        return None
    import datetime as _dt

    now = _dt.datetime.now(tz=_dt.UTC)
    if created.tzinfo is None:
        created = created.replace(tzinfo=_dt.UTC)
    return max(0.0, (now - created).total_seconds())


async def _pick_player(
    session_factory: async_sessionmaker[AsyncSession], room_id: str
) -> tuple[str, str] | None:
    async with session_factory() as db:
        rows = list((await db.execute(select(Player).where(Player.room_id == room_id))).scalars())
    humans = [p for p in rows if not p.is_ai]
    if not humans:
        return None
    p = humans[0]
    return p.id, p.nickname


async def _record_heartbeat_event(
    session_factory: async_sessionmaker[AsyncSession],
    room_id: str,
    player_id: str,
    text: str,
) -> None:
    async with session_factory() as db:
        db.add(
            Event(
                room_id=room_id,
                player_id=player_id,
                event_type="keeper.heartbeat",
                payload={"text": text[:500]},
            )
        )
        db.add(
            Event(
                room_id=room_id,
                player_id=player_id,
                event_type="narration.push",
                payload={"text": text},
            )
        )
        await db.commit()


async def maybe_fire_room(
    *,
    room_id: str,
    narrator: Narrator,
    session_factory: async_sessionmaker[AsyncSession],
    silence_seconds: float,
    min_interval_seconds: float,
    max_consecutive: int,
) -> bool:
    """对单房间尝试一次心跳。返回是否实际触发。"""
    from app.core.keeper.agent import KeeperAgent

    if not isinstance(narrator, KeeperAgent):
        return False

    if not ws_manager.has_connections(room_id):
        return False

    if pending_check_manager.first(room_id) is not None:
        return False

    now = _now()
    last_hb = _last_heartbeat_at.get(room_id, 0.0)
    if now - last_hb < min_interval_seconds:
        return False
    if _consecutive_heartbeats.get(room_id, 0) >= max_consecutive:
        return False

    last_act = _last_activity_at.get(room_id)
    if last_act is not None:
        silent_for = now - last_act
    else:
        age = await _last_event_age_seconds(session_factory, room_id)
        silent_for = age if age is not None else silence_seconds + 1
    if silent_for < silence_seconds:
        return False

    async with session_factory() as db:
        room = await db.get(Room, room_id)
        if room is None or room.phase != "InGame":
            return False

    player = await _pick_player(session_factory, room_id)
    if player is None:
        return False
    player_id, nickname = player

    token = action_lock_manager.try_acquire(room_id)
    if token is None:
        return False

    try:
        context = NarrationContext(
            utterance=HEARTBEAT_UTTERANCE,
            player_nickname=nickname,
            room_id=room_id,
            player_id=player_id,
            is_heartbeat=True,
        )
        outcome = await narrator.narrate(context)
        text = (outcome.text or "").strip()
        if not text:
            return False

        await _record_heartbeat_event(session_factory, room_id, player_id, text)
        from app.dto.ws import NarrationPushPayload, ServerEnvelope

        envelope = ServerEnvelope(
            type="narration.push",
            payload=NarrationPushPayload(text=text).model_dump(by_alias=True),
        )
        await ws_manager.broadcast(room_id, envelope.model_dump(by_alias=True))

        _last_heartbeat_at[room_id] = now
        _consecutive_heartbeats[room_id] = _consecutive_heartbeats.get(room_id, 0) + 1
        logger.info(
            "keeper_heartbeat_fired",
            room_id=room_id,
            consecutive=_consecutive_heartbeats[room_id],
            text_len=len(text),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — ticker 不能因单房失败退出
        logger.warning("keeper_heartbeat_failed", room_id=room_id, error=str(exc))
        return False
    finally:
        action_lock_manager.release(room_id, token)


async def scan_once(
    *,
    narrator: Narrator,
    session_factory: async_sessionmaker[AsyncSession],
    silence_seconds: float,
    min_interval_seconds: float,
    max_consecutive: int,
) -> int:
    """扫一遍活跃房间，返回触发次数。"""
    fired = 0
    for room_id in ws_manager.connected_room_ids():
        ok = await maybe_fire_room(
            room_id=room_id,
            narrator=narrator,
            session_factory=session_factory,
            silence_seconds=silence_seconds,
            min_interval_seconds=min_interval_seconds,
            max_consecutive=max_consecutive,
        )
        if ok:
            fired += 1
    return fired


async def heartbeat_loop(
    app: Any,
    *,
    interval_seconds: float = 30.0,
    silence_seconds: float = 100.0,
    min_interval_seconds: float = 300.0,
    max_consecutive: int = 2,
) -> None:
    """应用 lifespan 里启动的主循环；取消时干净退出。"""
    logger.info(
        "keeper_heartbeat_loop_started",
        interval=interval_seconds,
        silence=silence_seconds,
        min_interval=min_interval_seconds,
    )
    from app.core.db import async_session_factory

    try:
        while True:
            await asyncio.sleep(interval_seconds)
            narrator = getattr(app.state, "narrator", None)
            if narrator is None:
                continue
            try:
                n = await scan_once(
                    narrator=narrator,
                    session_factory=async_session_factory,
                    silence_seconds=silence_seconds,
                    min_interval_seconds=min_interval_seconds,
                    max_consecutive=max_consecutive,
                )
                if n:
                    logger.info("keeper_heartbeat_scan", fired=n)
            except Exception as exc:  # noqa: BLE001
                logger.warning("keeper_heartbeat_scan_error", error=str(exc))
    except asyncio.CancelledError:
        logger.info("keeper_heartbeat_loop_stopped")
        raise
