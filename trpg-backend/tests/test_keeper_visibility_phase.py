"""路线 5 Visibility + 路线 6 阶段/结局记账（纯代码路径，不打 LLM）。"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.decision import KeeperDecision, execute_side_effects
from app.core.keeper.module_loader import load_module
from app.core.keeper.phase import (
    ENDING_ID_KEY,
    PHASE_FINISHED,
    PHASE_INVESTIGATION,
    PHASE_KEY,
    format_phase_status,
    load_phase,
)
from app.core.keeper.prompts import format_turn_input
from app.core.keeper.tools import (
    AGENDA_FIRED_KEY,
    KeeperDeps,
    KeeperToolError,
    update_state_impl,
)
from app.core.keeper.visibility import (
    VISIBILITY_REVEALED_KEY,
    format_visibility_status,
    is_pair_revealed,
    load_revealed_visibility,
)
from app.models.room import Character, Player, Room

_FIXTURE = Path(__file__).parent / "fixtures" / "keeper_module.json"

_db_path = Path(tempfile.mkdtemp(prefix="trpg-vis-phase-")) / "t.db"
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
async def deps() -> KeeperDeps:
    module = load_module(_FIXTURE)
    async with _session_factory() as db:
        room = Room(room_code="VIS001", room_name="密级房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        actor = Player(room_id=room.id, nickname="调查者")
        db.add(actor)
        await db.flush()
        db.add(
            Character(
                room_id=room.id,
                player_id=actor.id,
                status="complete",
                name="调查者",
                occupation="记者",
                age=30,
                gender="男",
                attributes={
                    "STR": 50,
                    "CON": 50,
                    "SIZ": 50,
                    "DEX": 50,
                    "APP": 50,
                    "INT": 50,
                    "POW": 50,
                    "EDU": 50,
                    "LUCK": 50,
                },
                derived_stats={"HP": 10, "MP": 10, "SAN": 50, "MOV": 8},
                skills={"spot-hidden": 60},
            )
        )
        await db.commit()
        room_id, actor_id = room.id, actor.id

    return KeeperDeps(
        room_id=room_id,
        player_id=actor_id,
        session_factory=_session_factory,
        module=module,
        ruleset=build_coc7_ruleset(),
    )


def test_visibility_format_and_parse() -> None:
    module = load_module(_FIXTURE)
    text = format_visibility_status(module, [], observer_id="p1")
    assert "尚未揭开" in text
    assert "pair-butler-faces" in text

    revealed = [("pair-butler-faces", "*")]
    assert is_pair_revealed(revealed, "pair-butler-faces", "p1")
    text2 = format_visibility_status(module, revealed, observer_id="p1")
    assert "已揭开" in text2
    assert "尚未揭开" in text2  # 另一条 pair-hall-mud 仍封


@pytest.mark.asyncio
async def test_visibility_revealed_and_reserved_keys(deps: KeeperDeps) -> None:
    decision = KeeperDecision(
        thinking="挣得线索",
        visibility_revealed=["pair-butler-faces"],
        narration_guidance="可透露管家公开形象侧",
    )
    report, issues = await execute_side_effects(deps, decision)
    assert not issues
    assert any("密级揭开" in r for r in report)

    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        entries = load_revealed_visibility(room.keeper_state)
        assert is_pair_revealed(entries, "pair-butler-faces")

    with pytest.raises(KeeperToolError, match="系统记账"):
        await update_state_impl(deps, VISIBILITY_REVEALED_KEY, "hack")
    with pytest.raises(KeeperToolError, match="系统记账"):
        await update_state_impl(deps, AGENDA_FIRED_KEY, "hack")
    with pytest.raises(KeeperToolError, match="系统记账"):
        await update_state_impl(deps, PHASE_KEY, "opening")


@pytest.mark.asyncio
async def test_ending_reached_sets_finished(deps: KeeperDeps) -> None:
    decision = KeeperDecision(
        thinking="破案",
        ending_reached="solved",
        narration_guidance="终章",
    )
    report, issues = await execute_side_effects(deps, decision)
    assert not issues
    assert any("finished" in r or "结局" in r for r in report)

    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert load_phase(room.keeper_state) == PHASE_FINISHED
        assert room.keeper_state is not None
        assert room.keeper_state.get(ENDING_ID_KEY) == "solved"


@pytest.mark.asyncio
async def test_opening_complete_advances_phase(deps: KeeperDeps) -> None:
    decision = KeeperDecision(
        thinking="委托已接",
        opening_complete=True,
        narration_guidance="进入调查",
    )
    report, issues = await execute_side_effects(deps, decision)
    assert not issues
    assert any(PHASE_INVESTIGATION in r for r in report)

    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert load_phase(room.keeper_state) == PHASE_INVESTIGATION


def test_format_turn_includes_visibility_and_phase() -> None:
    text = format_turn_input(
        {"当前场景": "门厅"},
        ["玩家：你好"],
        ["调查者"],
        "调查者",
        "搜查门厅",
        agenda_status="- a · 夜",
        visibility_status="- pair-x：公开 a ↔ 真相 b",
        phase_status=format_phase_status("investigation"),
        is_heartbeat=True,
    )
    assert "主动推进轮" in text
    assert "密级配对状态" in text
    assert "对局阶段" in text
    assert "议程状态" in text


@pytest.mark.asyncio
async def test_heartbeat_gate_skips_without_keeper() -> None:
    from app.core.keeper import heartbeat as hb
    from app.core.narrator import FallbackNarrator

    hb.reset_heartbeat_state_for_tests()
    ok = await hb.maybe_fire_room(
        room_id="nope",
        narrator=FallbackNarrator(),
        session_factory=_session_factory,
        silence_seconds=0,
        min_interval_seconds=0,
        max_consecutive=2,
    )
    assert ok is False
