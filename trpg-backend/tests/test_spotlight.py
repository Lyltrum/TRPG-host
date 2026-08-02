"""聚光灯（exec/14 P5.2）：导演层从「整桌静了多久」扩成「谁多久没被点到」。

四人桌上话多的那位会一直占着回合，安静的那位可以整场都没被点到——而整桌
一点也不"静默"，旧的心跳判据完全察觉不到。这里验的是**选人**这一步：
谁最久没说话就选谁，超过阈值再强制注入聚光灯引导。
"""

import datetime as dt
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import Base
from app.core.keeper.narration.prose_discipline import inject_spotlight_guidance
from app.core.keeper.runtime.heartbeat import _pick_player
from app.models.event import Event
from app.models.room import Player, Room

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-spotlight-test-")) / "spotlight.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(room_code: str, spoke_minutes_ago: dict[str, float | None]) -> str:
    """建房 + 按「几分钟前说过话」造 action.submit 事件。None = 从没说过。"""
    now = dt.datetime.now(tz=dt.UTC)
    async with _session_factory() as db:
        room = Room(room_code=room_code, room_name="聚光灯房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        for nickname, minutes in spoke_minutes_ago.items():
            player = Player(room_id=room.id, nickname=nickname)
            db.add(player)
            await db.flush()
            if minutes is not None:
                db.add(
                    Event(
                        room_id=room.id,
                        player_id=player.id,
                        event_type="action.submit",
                        payload={"utterance": "我说点什么"},
                        created_at=now - dt.timedelta(minutes=minutes),
                    )
                )
        await db.commit()
        return room.id


async def test_picks_the_player_who_has_been_quiet_longest() -> None:
    """🔴 旧实现选的是 humans[0]（阿福）——话最多的那个。"""
    room_id = await _seed("SPOT01", {"阿福": 0.5, "阿贵": 12.0, "阿丙": 3.0})
    picked = await _pick_player(_session_factory, room_id, spotlight_seconds=480.0)
    assert picked is not None
    _pid, nickname, spotlighted = picked
    assert nickname == "阿贵"
    assert spotlighted is True  # 12 分钟 > 8 分钟阈值


async def test_never_spoken_ranks_first() -> None:
    """从没说过话的人最该被点到——不是最不该。"""
    room_id = await _seed("SPOT02", {"阿福": 0.1, "阿贵": 20.0, "阿丙": None})
    picked = await _pick_player(_session_factory, room_id, spotlight_seconds=480.0)
    assert picked is not None
    assert picked[1] == "阿丙"
    assert picked[2] is True


async def test_below_threshold_is_a_plain_heartbeat() -> None:
    """没到阈值就还是普通心跳，不注入聚光灯（行为与 P5.2 之前一致）。"""
    room_id = await _seed("SPOT03", {"阿福": 0.5, "阿贵": 2.0})
    picked = await _pick_player(_session_factory, room_id, spotlight_seconds=480.0)
    assert picked is not None
    assert picked[1] == "阿贵"
    assert picked[2] is False


async def test_empty_room_returns_none() -> None:
    room_id = await _seed("SPOT04", {})
    assert await _pick_player(_session_factory, room_id, spotlight_seconds=480.0) is None


def test_guidance_is_idempotent_and_keeps_original() -> None:
    once = inject_spotlight_guidance("裁决原始指引", "阿贵")
    assert "阿贵" in once and "裁决原始指引" in once
    assert inject_spotlight_guidance(once, "阿贵") == once
