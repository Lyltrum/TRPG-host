"""中途加入 / 中途离开的剧情落点。

结构性与纪律性在这一片分得很清，测试也按这条分：

- **硬的**：暂离的人不进在场名单（`agent` 按 `Player.away` 过滤）——模型
  想提他也提不了。
- **概率性**：把登场/离场写得好听（局面块请它圆一句），登记在 `exec/20`。
  这里只验"提示确实摆到了它眼前"，不验它写没写。
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys, situation_blocks
from app.core.keeper.capabilities.presence.state import (
    ANNOUNCED_ARRIVALS_KEY,
    PENDING_DEPARTURES_KEY,
    load_announced_arrivals,
    load_pending_departures,
    serialize_departures,
    unannounced_arrivals,
)
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.turn_executor import execute_side_effects
from app.models.room import Player, Room

_MODULE = ScenarioModule.model_validate(
    {
        "meta": {"id": "presence-fixture", "title": "试验模组"},
        "kp_truth": {"summary": "门厅有脚印。"},
        "player_intro": "你在门厅。",
        "nodes": [{"id": "hall", "title": "门厅", "kp_text": "地毯上有泥。"}],
    }
)


# ── 差集本身 ─────────────────────────────────────


def test_everyone_counts_as_new_on_the_very_first_turn() -> None:
    """开局第一轮全桌都算"刚到"，是有意的：开场那一拍本来就该介绍在座的人。"""
    assert unannounced_arrivals({}, (("p1", "阿福"), ("p2", "阿贵"))) == [
        ("p1", "阿福"),
        ("p2", "阿贵"),
    ]


def test_only_the_genuinely_new_one_is_left_afterwards() -> None:
    state = {ANNOUNCED_ARRIVALS_KEY: "p1, p2"}
    assert unannounced_arrivals(state, (("p1", "阿福"), ("p2", "阿贵"), ("p3", "阿铁"))) == [
        ("p3", "阿铁")
    ]


def test_departures_round_trip_through_storage() -> None:
    rows = [("p1", "阿福"), ("p2", "阿贵")]
    state = {PENDING_DEPARTURES_KEY: serialize_departures(rows)}
    assert load_pending_departures(state) == rows


def test_malformed_departure_rows_are_dropped_whole() -> None:
    assert load_pending_departures({PENDING_DEPARTURES_KEY: "没有分隔符, @只有名字, p1@"}) == []


# ── 局面块 ───────────────────────────────────────


def test_the_block_is_silent_when_nobody_came_or_went() -> None:
    state = {ANNOUNCED_ARRIVALS_KEY: "p1"}
    blocks = situation_blocks(_MODULE, state, players=(("p1", "阿福"),))
    assert not any("桌上的人变了" in body for _order, body in blocks)


def test_a_latecomer_shows_up_in_the_block() -> None:
    state = {ANNOUNCED_ARRIVALS_KEY: "p1"}
    blocks = situation_blocks(_MODULE, state, players=(("p1", "阿福"), ("p2", "阿贵")))
    body = "".join(b for _o, b in blocks if "桌上的人变了" in b)
    assert "阿贵" in body and "刚到" in body
    assert "阿福" not in body


def test_a_departure_shows_up_even_though_he_is_off_the_roster() -> None:
    """🔴 离场的人**不在 players 里**（那正是"暂离"的定义）——所以他的昵称
    必须在离场那一刻就存进 keeper_state，否则这里渲染不出名字。"""
    state = {
        ANNOUNCED_ARRIVALS_KEY: "p1, p2",
        PENDING_DEPARTURES_KEY: serialize_departures([("p2", "阿贵")]),
    }
    blocks = situation_blocks(_MODULE, state, players=(("p1", "阿福"),))
    body = "".join(b for _o, b in blocks if "桌上的人变了" in b)
    assert "阿贵" in body and "离场" in body
    # 用户的原话：暂时消失，不是写死
    assert "不要写死" in body or "随时可能回来" in body


def test_both_state_keys_are_reserved() -> None:
    assert ANNOUNCED_ARRIVALS_KEY in reserved_state_keys()
    assert PENDING_DEPARTURES_KEY in reserved_state_keys()


# ── 走真实执行链 ─────────────────────────────────

_db_path = Path(tempfile.mkdtemp(prefix="trpg-presence-test-")) / "presence.db"
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
async def table() -> tuple[KeeperDeps, str, str]:
    """房间里两个人：阿福（房主）与阿贵。返回 (deps, 阿福 id, 阿贵 id)。"""
    async with _session_factory() as db:
        room = Room(room_code="PRS001", room_name="进出房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        host = Player(room_id=room.id, nickname="阿福", is_host=True)
        guest = Player(room_id=room.id, nickname="阿贵")
        db.add_all([host, guest])
        await db.commit()
        room_id, host_id, guest_id = room.id, host.id, guest.id

    deps = KeeperDeps(
        room_id=room_id,
        player_id=host_id,
        session_factory=_session_factory,
        module=_MODULE,
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
        turn_player_ids=(host_id,),
        rng=random.Random(5),
    )
    return deps, host_id, guest_id


async def _state(deps: KeeperDeps) -> dict:
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        return dict(room.keeper_state or {})


async def test_running_a_turn_marks_everyone_present_as_announced(table) -> None:
    deps, host_id, guest_id = table

    report, issues = await execute_side_effects(deps, KeeperDecision())

    assert issues == []
    # 🔴 **执行报告恒为空**：它的语义是"世界变了什么"，而这里只是记账，
    # 局面块已经把同一件事摆到模型眼前了。第一版往报告里塞了一行，当场
    # 打红九条别的能力的既有测试。
    assert report == []
    assert set(load_announced_arrivals(await _state(deps))) == {host_id, guest_id}


async def test_the_second_turn_has_nothing_left_to_announce(table) -> None:
    """记账存在的理由：没有它，每一轮都会再介绍同一个人登场一次。

    验的是**局面块**（模型真正读到的那份），不是执行报告——报告恒为空。
    """
    deps, host_id, guest_id = table
    seated = ((host_id, "阿福"), (guest_id, "阿贵"))
    # 第一轮之前：两个人都还没被交代过，局面块要提
    blocks = situation_blocks(_MODULE, await _state(deps), players=seated)
    assert any("桌上的人变了" in body for _order, body in blocks)

    await execute_side_effects(deps, KeeperDecision())

    state = await _state(deps)
    assert unannounced_arrivals(state, seated) == []
    blocks = situation_blocks(_MODULE, state, players=seated)
    assert not any("桌上的人变了" in body for _order, body in blocks)


async def test_someone_who_walked_away_is_reported_once_then_cleared(table) -> None:
    deps, _host_id, guest_id = table
    await execute_side_effects(deps, KeeperDecision())
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        guest = await db.get(Player, guest_id)
        assert guest is not None
        guest.away = True
        room.keeper_state = {
            **(room.keeper_state or {}),
            PENDING_DEPARTURES_KEY: serialize_departures([(guest_id, "阿贵")]),
        }
        await db.commit()

    # 交代之前：局面块要提他离场
    blocks = situation_blocks(_MODULE, await _state(deps), players=((_host_id, "阿福"),))
    assert any("阿贵" in body for _o, body in blocks if "桌上的人变了" in body)

    await execute_side_effects(deps, KeeperDecision())

    # 交代过就不再挂着——否则守秘人会一轮一轮地重复送走同一个人
    assert load_pending_departures(await _state(deps)) == []
    blocks = situation_blocks(_MODULE, await _state(deps), players=((_host_id, "阿福"),))
    assert not any("桌上的人变了" in body for _o, body in blocks)


async def test_coming_back_makes_him_a_newcomer_again(table) -> None:
    """回来要重新被交代一次登场——他上一轮已经从故事里退出去了。"""
    deps, _host_id, guest_id = table
    await execute_side_effects(deps, KeeperDecision())
    async with _session_factory() as db:
        guest = await db.get(Player, guest_id)
        assert guest is not None
        guest.away = True
        await db.commit()
    # 他不在场的那一轮：记账把在场的人重算，他不在其中
    await execute_side_effects(deps, KeeperDecision())
    # 回来。**把他从「已交代登场」里摘出去是服务层做的**
    # （`room_service.set_player_away`）——这一片只负责"没交代过的就交代"，
    # 谁该重新排队不归它管。这里手动复现服务层那一步。
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        state = dict(room.keeper_state or {})
        state[ANNOUNCED_ARRIVALS_KEY] = ", ".join(
            pid for pid in load_announced_arrivals(state) if pid != guest_id
        )
        room.keeper_state = state
        guest = await db.get(Player, guest_id)
        assert guest is not None
        guest.away = False
        await db.commit()

    blocks = situation_blocks(_MODULE, await _state(deps), players=((guest_id, "阿贵"),))
    assert any("阿贵" in body and "刚到" in body for _o, body in blocks)
