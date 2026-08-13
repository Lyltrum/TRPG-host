"""暂停（`exec/35`）必须挡住世界心跳。

心跳是**唯一一条不需要玩家动手**就能推进世界的路径：挡住玩家提交挡不住它。
大家去上厕所、点外卖的那几分钟，恰好也是"整桌静默"最容易满足的时候——
不挡这一道，休息期间世界反而推得比平时更勤。

这里验的是 `maybe_fire_room` 里那一道 `if room.paused`：除了暂停位之外
一切条件都满足，暂停时**一次都不能调到 narrate**，取消暂停后照常触发。
"""

import datetime as dt
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import Base
from app.core.keeper.runtime import heartbeat as heartbeat_module
from app.core.keeper.runtime.agent import KeeperAgent
from app.core.keeper.runtime.heartbeat import maybe_fire_room, reset_heartbeat_state_for_tests
from app.core.narration.contract import NarrationOutcome
from app.models.event import Event
from app.models.room import Player, Room

_db_path = Path(tempfile.mkdtemp(prefix="trpg-heartbeat-paused-test-")) / "paused.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    reset_heartbeat_state_for_tests()
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


class _RecordingAgent(KeeperAgent):
    """只记"被叫过几次"的替身：isinstance 检查要求它真是 KeeperAgent。"""

    def __init__(self) -> None:  # noqa: D107 — 故意不走真构造（不建 LLM 客户端）
        self.calls = 0

    async def narrate(self, context) -> NarrationOutcome:  # type: ignore[override]
        self.calls += 1
        return NarrationOutcome(text="夜更深了。")


async def _seed(*, paused: bool) -> str:
    """一个满足全部心跳前置条件的房间：InGame、有人、十分钟没动静。"""
    async with _session_factory() as db:
        room = Room(
            room_code="PAUSE1",
            room_name="休息中的房间",
            max_players=4,
            phase="InGame",
            paused=paused,
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="阿福")
        db.add(player)
        await db.flush()
        db.add(
            Event(
                room_id=room.id,
                player_id=player.id,
                event_type="action.submit",
                payload={"utterance": "我看看桌子底下"},
                created_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=10),
            )
        )
        await db.commit()
        return room.id


@pytest.fixture
def _connected(monkeypatch):
    """心跳只扫有 WS 连接的房间；这里把连接与广播都换成空操作。"""
    monkeypatch.setattr(heartbeat_module.ws_manager, "has_connections", lambda room_id: True)

    async def _noop_broadcast(room_id, payload):
        return None

    monkeypatch.setattr(heartbeat_module.ws_manager, "broadcast", _noop_broadcast)


async def _fire(room_id: str, agent: _RecordingAgent) -> bool:
    return await maybe_fire_room(
        room_id=room_id,
        narrator=agent,
        session_factory=_session_factory,
        silence_seconds=1.0,
        min_interval_seconds=0.0,
        max_consecutive=99,
    )


async def test_paused_room_never_fires_heartbeat(_connected) -> None:
    room_id = await _seed(paused=True)
    agent = _RecordingAgent()

    assert await _fire(room_id, agent) is False
    # 返回 False 还不够：要确认它是**在调模型之前**被挡住的。
    assert agent.calls == 0

    async with _session_factory() as db:
        events = (await db.execute(Event.__table__.select())).fetchall()
    assert all(row.event_type != "narration.push" for row in events)


async def test_same_room_fires_once_unpaused(_connected) -> None:
    """同一组前置条件下取消暂停就会触发——证明上一条挡住的确实是 paused。"""
    room_id = await _seed(paused=False)
    agent = _RecordingAgent()

    assert await _fire(room_id, agent) is True
    assert agent.calls == 1
