"""线索揭示（clue_reveal，原路线 5 Visibility）的验收——纯代码路径，不打 LLM。"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys, situation_blocks
from app.core.keeper.capabilities.agenda.state import AGENDA_FIRED_KEY
from app.core.keeper.capabilities.clue_reveal.pairs import (
    CLUES_REVEALED_KEY,
    format_clue_status,
    is_pair_revealed,
    load_revealed_clues,
    pairs_reached_by_nodes,
)
from app.core.keeper.capabilities.world_state.executor import update_state_impl
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.narration.prompts import format_turn_input
from app.core.keeper.runtime.deps import KeeperDeps, KeeperToolError
from app.core.keeper.runtime.location_state import PLAYER_LOCATION_KEY
from app.core.keeper.runtime.phase import (
    PHASE_INVESTIGATION,
    PHASE_KEY,
    format_phase_status,
)
from app.core.keeper.runtime.turn_executor import execute_side_effects
from app.models.room import Character, Player, Room

# 模组夹具几片能力共用，仍集中放在 tests/fixtures
#: 🔴 用锚点找，不数层数：`exec/27` 阶段 5 挪目录时 `catalog.py` 的
#: `parents[3]` 当场指错一层，症状只是一条用例**静默 skip**（全套照样绿）。
_TESTS_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "trpg-backend") / "tests"
_FIXTURE = _TESTS_DIR / "fixtures" / "keeper_module.json"

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
        reserved_state_keys=reserved_state_keys(),
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


def test_only_pairs_whose_secret_side_is_a_real_node_can_be_reached() -> None:
    """纯函数两头：真相侧是节点的能被"到过"点亮，不是节点的永远不会。

    夹具刚好一头一个：`pair-hall-mud` 的真相侧是 `cellar`（真节点），
    `pair-butler-faces` 的真相侧是 `butler-secret`（不是节点）。
    """
    module = load_module(_FIXTURE)
    assert pairs_reached_by_nodes(module, [], {"cellar"}) == ["pair-hall-mud"]
    # 没到过 → 一条都不给
    assert pairs_reached_by_nodes(module, [], {"hall"}) == []
    # 已经揭开过 → 不重复给（幂等）
    assert pairs_reached_by_nodes(module, [("pair-hall-mud", "*")], {"cellar"}) == []
    # 真相侧不是节点的那条，走遍全图也点不亮
    assert "pair-butler-faces" not in pairs_reached_by_nodes(module, [], {"hall", "cellar"})


@pytest.mark.asyncio
async def test_reaching_the_secret_node_reveals_the_pair_without_the_model(
    deps: KeeperDeps,
) -> None:
    """🔴 真人实测 2026-08-14 的回归：整局 106 次裁决 `clues_revealed` 一次没写过，
    收尾门的分子恒为 0。这里断言**模型一个字不写**也能揭开。"""
    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        room.keeper_state = {PLAYER_LOCATION_KEY: f"{deps.player_id}@cellar"}
        await db.commit()

    decision = KeeperDecision(thinking="玩家下到地窖", narration_guidance="描述地窖")
    assert not decision.clues_revealed  # 前提：模型确实什么都没写
    report, issues = await execute_side_effects(deps, decision)
    assert not issues

    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        entries = load_revealed_clues(room.keeper_state)
    assert is_pair_revealed(entries, "pair-hall-mud")
    assert not is_pair_revealed(entries, "pair-butler-faces")
    assert any("密级揭开" in r for r in report)


@pytest.mark.asyncio
async def test_standing_somewhere_else_reveals_nothing(deps: KeeperDeps) -> None:
    """必然失败样本：人在门厅，两条配对一条都不该动。"""
    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        room.keeper_state = {PLAYER_LOCATION_KEY: f"{deps.player_id}@hall"}
        await db.commit()

    _, issues = await execute_side_effects(
        deps, KeeperDecision(thinking="人在门厅", narration_guidance="描述门厅")
    )
    assert not issues

    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert load_revealed_clues(room.keeper_state) == []


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
