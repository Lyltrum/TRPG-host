"""per-player 位置（exec/14 P5.2a）：编解码 + 分组 + 写入侧 + 检定护栏按人判定。

fixture 沿用原创迷你庄园失窃案（tests/fixtures/keeper_module.json），
与任何第三方模组原文无关。
"""

import random
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys
from app.core.keeper.capabilities.movement.schema import PlayerMove
from app.core.keeper.capabilities.world_state.executor import update_state_impl
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.runtime.deps import KeeperDeps, KeeperToolError
from app.core.keeper.runtime.location_state import (
    PLAYER_LOCATION_KEY,
    format_party_locations,
    group_players,
    is_party_split,
    load_player_locations,
    location_of,
    serialize_player_locations,
)
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY
from app.core.keeper.runtime.turn_executor import create_pending_checks, execute_side_effects
from app.models.room import Character, Player, Room

_FIXTURE_MODULE = Path(__file__).parent / "fixtures" / "keeper_module.json"

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-location-test-")) / "location.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _character(room_id: str, player_id: str, name: str) -> Character:
    return Character(
        room_id=room_id,
        player_id=player_id,
        status="complete",
        name=name,
        occupation="私家侦探",
        age=32,
        gender="男",
        attributes={
            "STR": 60,
            "CON": 50,
            "SIZ": 50,
            "DEX": 70,
            "APP": 50,
            "INT": 80,
            "POW": 50,
            "EDU": 70,
            "LUCK": 55,
        },
        derived_stats={"HP": 10, "MP": 10, "SAN": 50, "MOV": 8},
        skills={"spot-hidden": 70, "library-use": 60},
    )


@pytest.fixture
async def party() -> tuple[KeeperDeps, str, str]:
    """两人房：阿福（本轮发起者）+ 阿贵。返回 (deps, 阿福 id, 阿贵 id)。"""
    async with _session_factory() as db:
        room = Room(room_code="LOC001", room_name="分头房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        a = Player(room_id=room.id, nickname="阿福", is_host=True)
        b = Player(room_id=room.id, nickname="阿贵")
        db.add_all([a, b])
        await db.flush()
        db.add_all([_character(room.id, a.id, "侦探福"), _character(room.id, b.id, "记者贵")])
        await db.commit()
        room_id, a_id, b_id = room.id, a.id, b.id

    deps = KeeperDeps(
        room_id=room_id,
        player_id=a_id,
        session_factory=_session_factory,
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
        turn_player_ids=(a_id,),
        rng=random.Random(42),
    )
    return deps, a_id, b_id


async def _state(deps: KeeperDeps) -> dict:
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        return dict(room.keeper_state or {})


# ── 1. 编解码 ───────────────────────────────────────


def test_roundtrip_and_ignores_garbage() -> None:
    assert load_player_locations({PLAYER_LOCATION_KEY: "p1@hall, p2@cellar"}) == {
        "p1": "hall",
        "p2": "cellar",
    }
    # 缺 @ / 空段 / 空值一律丢弃，不产生半条记录
    assert load_player_locations({PLAYER_LOCATION_KEY: "p1@hall, , broken, p3@"}) == {"p1": "hall"}
    assert load_player_locations(None) == {}
    assert serialize_player_locations({"p1": "hall"}) == "p1@hall"


def test_location_falls_back_to_room_pointer() -> None:
    """没被单独定位过的人 = 跟大部队在一起（有定义的默认值，不是静默兜底）。"""
    state = {CURRENT_NODE_KEY: "hall", PLAYER_LOCATION_KEY: "p2@cellar"}
    assert location_of(state, "p1") == "hall"
    assert location_of(state, "p2") == "cellar"
    # 房间级指针也没有 → None，**不得**当成"跟谁都在一起"
    assert location_of({}, "p1") is None


def test_group_players_keeps_input_order_and_isolates_unknown() -> None:
    state = {CURRENT_NODE_KEY: "hall", PLAYER_LOCATION_KEY: "p2@cellar"}
    assert group_players(state, ["p1", "p2", "p3"]) == [("hall", ["p1", "p3"]), ("cellar", ["p2"])]
    # 位置全未知时是一组，不是三组
    assert group_players({}, ["p1", "p2"]) == [(None, ["p1", "p2"])]
    assert is_party_split(state, ["p1", "p2"]) is True
    assert is_party_split({CURRENT_NODE_KEY: "hall"}, ["p1", "p2"]) is False


def test_format_party_locations_is_empty_when_together() -> None:
    """退化保证：未分头 → 空串 → 局面块整块不渲染，prompt 与 P5.2 之前一致。"""
    module = load_module(_FIXTURE_MODULE)
    together = {CURRENT_NODE_KEY: "hall"}
    assert format_party_locations(module, together, [("p1", "阿福"), ("p2", "阿贵")]) == ""

    split = {CURRENT_NODE_KEY: "hall", PLAYER_LOCATION_KEY: "p2@cellar"}
    text = format_party_locations(module, split, [("p1", "阿福"), ("p2", "阿贵")])
    assert "阿福" in text and "阿贵" in text and "cellar" in text


# ── 2. 写入侧 ───────────────────────────────────────


async def test_current_node_moves_everyone_standing_with_the_speaker(party) -> None:
    """🔴 跟你站在一起的人跟你一起走（exec/19 #37 真人实测打脸后改的）。

    最初只挪"本轮发言的人"，结果两人肩并肩站着也会被判成分头：
    先张家豪发言（他拿到显式条目），再凌铭辉发言（房间指针跟着变，但张家豪
    的旧条目不再回落）→ 叙事分段投递、一个人什么都收不到，连裁决器都读着
    错误的「各自所在」少发了检定。
    """
    deps, a_id, b_id = party
    # 第一轮：阿福发言。两人此刻都没有显式位置（都回落 None）→ 一起走。
    _report, issues = await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    assert issues == []
    assert load_player_locations(await _state(deps)) == {a_id: "hall", b_id: "hall"}

    # 第二轮：还是阿福发言。阿贵已有显式条目「hall」，与阿福同处 → 仍然一起走。
    await execute_side_effects(deps, KeeperDecision(current_node_id="cellar"))
    state = await _state(deps)
    assert load_player_locations(state) == {a_id: "cellar", b_id: "cellar"}
    assert state[CURRENT_NODE_KEY] == "cellar"
    assert is_party_split(state, [a_id, b_id]) is False


async def test_current_node_does_not_drag_along_someone_who_split_off(party) -> None:
    """真分头的人不该被隔空传送走——这是当初那条顾虑，它仍然成立。"""
    deps, a_id, b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    # 阿贵单独去地下室
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="cellar")])
    )
    # 阿福（在门厅）继续走动：阿贵不在他那儿，不跟着走
    await execute_side_effects(deps, KeeperDecision(current_node_id="hidden-safe"))
    state = await _state(deps)
    assert load_player_locations(state) == {a_id: "hidden-safe", b_id: "cellar"}
    assert is_party_split(state, [a_id, b_id]) is True


async def test_moves_override_current_node(party) -> None:
    """分头：大家进地下室，阿贵单独留在门厅。顺序必须是 current_node → moves。"""
    deps, a_id, b_id = party
    deps.turn_player_ids = (a_id, b_id)
    decision = KeeperDecision(
        current_node_id="cellar",
        moves=[PlayerMove(player="阿贵", node_id="hall")],
    )
    report, issues = await execute_side_effects(deps, decision)
    assert issues == []
    assert any("阿贵" in line for line in report)
    state = await _state(deps)
    assert load_player_locations(state) == {a_id: "cellar", b_id: "hall"}
    assert is_party_split(state, [a_id, b_id]) is True


async def test_move_with_unknown_node_or_player_is_issue_not_crash(party) -> None:
    deps, a_id, _b_id = party
    decision = KeeperDecision(
        moves=[
            PlayerMove(player="阿贵", node_id="no-such-node"),
            PlayerMove(player="查无此人", node_id="hall"),
        ]
    )
    _report, issues = await execute_side_effects(deps, decision)
    assert len(issues) == 2
    assert any("no-such-node" in i for i in issues)
    assert any("查无此人" in i for i in issues)
    assert load_player_locations(await _state(deps)) == {}


async def test_player_location_key_is_reserved_from_state_updates(party) -> None:
    """LLM 的 state_updates 改不动逐人位置表。"""
    deps, _a_id, _b_id = party
    with pytest.raises(KeeperToolError):
        await update_state_impl(deps, PLAYER_LOCATION_KEY, "任意@任意")


# ── 3. 检定护栏按人判定 ─────────────────────────────


async def test_check_guard_uses_each_players_own_location(party) -> None:
    """🔴 分头后不能用房间级指针去卡另一个人的检定。

    fixture 里 hall 标注了检定点 spot-hidden、cellar 没标注 checks（即兴层放行）。
    阿福在 cellar、阿贵在 hall：
    - 阿福掷「图书馆使用」→ 所在节点无 checks → 放行；
    - 若按房间级指针（hall）判定，这一条会被 hall 的 checks 否掉。
    """
    deps, a_id, b_id = party
    deps.turn_player_ids = (a_id,)
    await execute_side_effects(deps, KeeperDecision(current_node_id="cellar"))
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="hall")])
    )

    decision = KeeperDecision.model_validate(
        {
            "checks": [
                {"skill_id": "library-use", "player": "阿福"},
                {"skill_id": "library-use", "player": "阿贵"},
            ]
        }
    )
    pending, issues = await create_pending_checks(deps, decision)
    assert [p.player_id for p in pending] == [a_id]
    # 阿贵那条被他自己所在的门厅护栏否掉（门厅只标注了 spot-hidden）
    assert len(issues) == 1
    assert "library-use" in issues[0] and "门厅" in issues[0]
