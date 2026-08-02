"""线索揭示（clue_reveal，原路线 5 Visibility）的验收——纯代码路径，不打 LLM。"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import situation_blocks
from app.core.keeper.capabilities.agenda.state import AGENDA_FIRED_KEY
from app.core.keeper.capabilities.clue_reveal.pairs import (
    CLUES_REVEALED_KEY,
    format_clue_status,
    is_pair_revealed,
    load_revealed_clues,
)
from app.core.keeper.decision import KeeperDecision
from app.core.keeper.deps import KeeperDeps, KeeperToolError
from app.core.keeper.module_loader import load_module
from app.core.keeper.phase import (
    PHASE_INVESTIGATION,
    PHASE_KEY,
    format_phase_status,
)
from app.core.keeper.prompts import format_turn_input
from app.core.keeper.tools import update_state_impl
from app.core.keeper.turn_executor import execute_side_effects
from app.models.room import Character, Player, Room

# 模组夹具几片能力共用，仍集中放在 tests/fixtures
_FIXTURE = Path(__file__).resolve().parents[5] / "tests" / "fixtures" / "keeper_module.json"

_db_path = Path(tempfile.mkdtemp(prefix="trpg-clue-")) / "t.db"
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
        room = Room(room_code="CLU001", room_name="密级房", max_players=4, phase="InGame")
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
    text = format_clue_status(module, [], observer_id="p1")
    assert "尚未揭开" in text
    assert "pair-butler-faces" in text

    revealed = [("pair-butler-faces", "*")]
    assert is_pair_revealed(revealed, "pair-butler-faces", "p1")
    text2 = format_clue_status(module, revealed, observer_id="p1")
    assert "已揭开" in text2
    assert "尚未揭开" in text2  # 另一条 pair-hall-mud 仍封


@pytest.mark.asyncio
async def test_visibility_revealed_and_reserved_keys(deps: KeeperDeps) -> None:
    decision = KeeperDecision(
        thinking="挣得线索",
        clues_revealed=["pair-butler-faces"],
        narration_guidance="可透露管家公开形象侧",
    )
    report, issues = await execute_side_effects(deps, decision)
    assert not issues
    assert any("密级揭开" in r for r in report)

    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        entries = load_revealed_clues(room.keeper_state)
        assert is_pair_revealed(entries, "pair-butler-faces")

    with pytest.raises(KeeperToolError, match="系统记账"):
        await update_state_impl(deps, CLUES_REVEALED_KEY, "hack")
    with pytest.raises(KeeperToolError, match="系统记账"):
        await update_state_impl(deps, AGENDA_FIRED_KEY, "hack")
    with pytest.raises(KeeperToolError, match="系统记账"):
        await update_state_impl(deps, PHASE_KEY, "opening")


def test_situation_blocks_carry_the_clue_status() -> None:
    """密级配对状态走 situation 钩子进局面块，位置由 order 决定。"""
    module = load_module(_FIXTURE)
    blocks = situation_blocks(module, None, observer_id="p1")
    text = format_turn_input(
        {"当前场景": "门厅"},
        ["玩家：你好"],
        ["调查者"],
        "调查者",
        "搜查门厅",
        phase_status=format_phase_status(PHASE_INVESTIGATION),
        capability_blocks=blocks,
        is_heartbeat=True,
    )
    assert "主动推进轮" in text
    assert "密级配对状态" in text
    assert "对局阶段" in text
