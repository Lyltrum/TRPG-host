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

from app.core.keeper.runtime.pending import ROLL_KINDS, pending_decision_manager
from app.core.narration.contract import NarrationContext, Narrator
from app.models.event import Event
from app.models.room import Player, Room
from app.service.action_lock import action_lock_manager
from app.service.ws_manager import manager as ws_manager

logger = structlog.get_logger()

HEARTBEAT_UTTERANCE = "（时间悄然流逝）"

#: 一名调查员多久没发过言就算"被冷落"，心跳轮要把镜头转向他（P5.2 聚光灯）。
#: 取值比 silence_seconds 大一个量级：整桌静默是"没人说话"，被冷落是"别人在
#: 说、就他没说上话"，后者必然要观察更长一段时间才成立。
_DEFAULT_SPOTLIGHT_SECONDS = 480.0

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
    session_factory: async_sessionmaker[AsyncSession], room_id: str, spotlight_seconds: float
) -> tuple[str, str, bool] | None:
    """挑本轮心跳该对着谁说话。返回 (player_id, 昵称, 是否触发聚光灯)。

    🔴 exec/14 P5.2 聚光灯：导演层原本只问"整桌静了多久"，选人是 `humans[0]`
    ——四人桌上话多的那位会一直占着回合，安静的那位可以整场都没被点到，而
    系统完全察觉不到（整桌一点也不静默）。这里改成**谁最久没说话就选谁**：
    按各人最后一条 `action.submit` 的时间排，从没说过话的排最前。

    超过 `spotlight_seconds` 才算"被冷落"（第三个返回值），此时 keeper 会
    强制注入聚光灯引导；没超过就是普通心跳，行为与 P5.2 之前一致。
    """
    import datetime as _dt

    async with session_factory() as db:
        rows = list((await db.execute(select(Player).where(Player.room_id == room_id))).scalars())
        # 🔴 这里排除 AI 玩家是**有意的语义**，不是"AI 还不存在"时的顺手防御
        # （exec/21 第一层裁决表里唯一保留排除的一处）：聚光灯的职责是照顾
        # **被冷落的真人**——四人桌上话多的人会一直占着回合，安静的那位可能
        # 整场不被点到。AI 玩家不会因为没被点到而觉得无聊，把它算进来只会挤掉
        # 真人的镜头。
        humans = [p for p in rows if not p.is_ai]
        if not humans:
            return None
        result = await db.execute(
            select(Event.player_id, Event.created_at)
            .where(Event.room_id == room_id, Event.event_type == "action.submit")
            .order_by(Event.created_at.desc(), Event.id.desc())
        )
        last_spoke: dict[str, _dt.datetime] = {}
        for speaker_id, created_at in result.tuples():
            if speaker_id and speaker_id not in last_spoke:
                last_spoke[speaker_id] = created_at

    now = _dt.datetime.now(tz=_dt.UTC)

    def _silent_for(player: Player) -> float:
        created = last_spoke.get(player.id)
        if created is None:
            # 从没说过话 = 最该被点到。用一个必然大于任何真实沉默时长的值，
            # 而不是 0 —— 这个方向弄反，正好把最该照顾的人排到最后。
            return float("inf")
        if created.tzinfo is None:
            created = created.replace(tzinfo=_dt.UTC)
        return max(0.0, (now - created).total_seconds())

    # 并列时保持房间成员顺序（max 取第一个最大值），选人是确定性的
    target = max(humans, key=_silent_for)
    return target.id, target.nickname, _silent_for(target) >= spotlight_seconds


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
    spotlight_seconds: float = _DEFAULT_SPOTLIGHT_SECONDS,
) -> bool:
    """对单房间尝试一次心跳。返回是否实际触发。"""
    from app.core.keeper.runtime.agent import KeeperAgent

    if not isinstance(narrator, KeeperAgent):
        return False

    if not ws_manager.has_connections(room_id):
        return False

    async with session_factory() as db:
        if await pending_decision_manager.first(db, room_id, ROLL_KINDS) is not None:
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

    player = await _pick_player(session_factory, room_id, spotlight_seconds)
    if player is None:
        return False
    player_id, nickname, spotlighted = player

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
            spotlight_nickname=nickname if spotlighted else None,
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
            spotlight=nickname if spotlighted else None,
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
