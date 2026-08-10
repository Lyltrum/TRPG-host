"""检定卡片与结果按位置投递（2026-08-10 多人实测）。

实测证据：阿福在地下室掷侦察，**在客厅的阿贵也收到了那张检定卡片和结果**。
叙事那一半早就按位置裁了（`_deliver_narration_segments`），检定这一半还是
`manager.broadcast` 全房间——**同一件事的两头，一头做了一头没做**，
跟「发起注册表化、结算写死 if/else」是同一个形状。

🔴 为什么测在这一层：同房间双 WS 客户端 pytest 做不了（TestClient 每个连接
独立事件循环，跨循环广播挂死，项目已记）。所以这里不测"谁的 socket 收到了
字节"，测的是**投递决策**——`_send_to_colocated` 把信封交给了 broadcast
还是 send_to_players、受众是谁。真正的端到端由 e2e/多人脚本覆盖。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.controller import ws as ws_module
from app.core.db import Base
from app.core.keeper.runtime.location_state import PLAYER_LOCATION_KEY
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY
from app.core.narration.contract import (
    CheckRequestNotice,
    CheckResultNotice,
    StatChangeNotice,
)
from app.models.room import Player, Room

_db_path = Path(tempfile.mkdtemp(prefix="trpg-check-deliver-")) / "deliver.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def sent(monkeypatch) -> list[tuple[str, list[str] | None]]:
    """拦住投递层，记下每次是广播还是定向、发给了谁。"""
    calls: list[tuple[str, list[str] | None]] = []

    async def _broadcast(room_id, envelope):  # noqa: ANN001
        calls.append((envelope["type"], None))

    async def _send_to_players(room_id, player_ids, envelope):  # noqa: ANN001
        calls.append((envelope["type"], sorted(player_ids)))

    monkeypatch.setattr(ws_module.manager, "broadcast", _broadcast)
    monkeypatch.setattr(ws_module.manager, "send_to_players", _send_to_players)
    return calls


async def _room(state: dict | None) -> tuple[str, str, str]:
    async with _session_factory() as db:
        room = Room(room_code="DLV001", room_name="分头房", max_players=4, phase="InGame")
        room.keeper_state = state
        db.add(room)
        await db.flush()
        a = Player(room_id=room.id, nickname="阿福", is_host=True)
        b = Player(room_id=room.id, nickname="阿贵")
        db.add_all([a, b])
        await db.commit()
        return room.id, a.id, b.id


def _request(player_id: str) -> CheckRequestNotice:
    return CheckRequestNotice(
        check_request_id="req-1",
        kind="skill",
        player_id=player_id,
        player_nickname="阿福",
        skill="侦察",
    )


def _result(player_id: str) -> CheckResultNotice:
    return CheckResultNotice(
        check_request_id="req-1",
        kind="skill",
        player_id=player_id,
        skill="侦察",
        rolled=39,
        target=25,
        level="失败",
    )


async def test_split_party_only_the_rollers_group_sees_the_card(sent) -> None:
    """🔴 实测复现：阿福在地下室掷骰，阿贵在客厅**不该**看见那张卡片和结果。"""
    room_id, a_id, b_id = await _room({CURRENT_NODE_KEY: "hall"})
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        room.keeper_state = {
            CURRENT_NODE_KEY: "hall",
            PLAYER_LOCATION_KEY: f"{a_id}@cellar, {b_id}@hall",
        }
        await db.commit()

    async with _session_factory() as db:
        await ws_module._broadcast_check_request(room_id, _request(a_id), db)
        await ws_module._broadcast_check_result(room_id, _result(a_id), db)

    assert sent == [("check.request", [a_id]), ("check.result", [a_id])]


async def test_together_still_broadcasts_to_the_whole_room(sent) -> None:
    """退化保证：没分头时行为与改动前逐字一致（全房间广播）。"""
    room_id, a_id, _b_id = await _room({CURRENT_NODE_KEY: "hall"})
    async with _session_factory() as db:
        await ws_module._broadcast_check_request(room_id, _request(a_id), db)
        await ws_module._broadcast_check_result(room_id, _result(a_id), db)
    assert sent == [("check.request", None), ("check.result", None)]


async def test_without_a_session_it_falls_back_to_broadcast(sent) -> None:
    """拿不到会话的调用点（现在没有）退化成广播——但要是显式的，不是静默的。"""
    room_id, a_id, _b_id = await _room({CURRENT_NODE_KEY: "hall"})
    await ws_module._broadcast_check_request(room_id, _request(a_id))
    assert sent == [("check.request", None)]


# ── HP 变更（exec/33 §3.1）：跟检定同一套受众 ──


def _stat(player_id: str) -> StatChangeNotice:
    return StatChangeNotice(player_id=player_id, hp=7, hp_max=12, reason="被壁橱里的东西抓伤")


async def test_split_party_only_the_hurt_group_sees_the_hp_change(sent) -> None:
    """🔴 payload 里带着 `reason`，全房间推等于告诉另一组**这边有人受伤了、
    还是被什么伤的**。掉血是虚构世界里发生的事，按位置裁。"""
    room_id, a_id, b_id = await _room({CURRENT_NODE_KEY: "hall"})
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        room.keeper_state = {
            CURRENT_NODE_KEY: "hall",
            PLAYER_LOCATION_KEY: f"{a_id}@cellar, {b_id}@hall",
        }
        await db.commit()

    async with _session_factory() as db:
        await ws_module._broadcast_stat_change(room_id, _stat(a_id), db)

    assert sent == [("character.stat_changed", [a_id])]


async def test_hp_change_still_broadcasts_when_together(sent) -> None:
    """退化保证：没分头时与 §3.1 之前逐字一致（全房间）。"""
    room_id, a_id, _b_id = await _room({CURRENT_NODE_KEY: "hall"})
    async with _session_factory() as db:
        await ws_module._broadcast_stat_change(room_id, _stat(a_id), db)
    assert sent == [("character.stat_changed", None)]


# ── 分头叙事的 delta（exec/33 §3.2）：ws 这一头 ──


async def test_segment_delta_sink_only_reaches_that_segments_audience(sent) -> None:
    """🔴 agent 把受众传对只是一半，投递这一头也得真的按它发。

    两头分开测是刻意的：agent 那条用例（test_narration_fanout）验的是"每段
    的 delta 受众 == 那一段的受众"，这条验的是"拿到受众之后没有退回广播"。
    只测一头的话，把这里改回 `manager.broadcast` 不会有任何东西变红——而那
    正是并行叙事落地那天的泄露形态。
    """
    room_id, a_id, _b_id = await _room({CURRENT_NODE_KEY: "hall"})
    sink = ws_module._segment_delta_sink_factory(room_id)("evt-1", (a_id,))
    await sink(0, "屋后有脚印。")
    assert sent == [("narration.delta", [a_id])]
