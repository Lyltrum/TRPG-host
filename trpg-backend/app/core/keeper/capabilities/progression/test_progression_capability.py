"""对局阶段推进与结局收束（progression，原路线 6）的验收。

🔴 阶段**值**不归本能力（`keeper/phase.py` 的模块说明写了原因），
这里验的是「什么时候推进」：两个裁决字段落到 keeper_state 的结果。"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys
from app.core.keeper.capabilities.progression.endings import format_endings_status
from app.core.keeper.decision import KeeperDecision
from app.core.keeper.deps import KeeperDeps
from app.core.keeper.module_loader import load_module
from app.core.keeper.phase import (
    ENDING_ID_KEY,
    PHASE_FINISHED,
    PHASE_INVESTIGATION,
    load_phase,
)
from app.core.keeper.registry import SituationContext
from app.core.keeper.turn_executor import execute_side_effects
from app.models.room import Character, Player, Room

# 模组夹具几片能力共用，仍集中放在 tests/fixtures
_FIXTURE = Path(__file__).resolve().parents[5] / "tests" / "fixtures" / "keeper_module.json"
_MODULE = load_module(_FIXTURE)

_db_path = Path(tempfile.mkdtemp(prefix="trpg-prog-")) / "t.db"
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
        room = Room(room_code="PRG001", room_name="密级房", max_players=4, phase="InGame")
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
        assert room is not None
        assert load_phase(room.keeper_state) == PHASE_INVESTIGATION


@pytest.mark.asyncio
async def test_heartbeat_gate_skips_without_keeper() -> None:
    from app.core.keeper import heartbeat as hb
    from app.core.narration.fallback import FallbackNarrator

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


# ── #47 结局条件进局面块 ────────────────────────────


def test_endings_status_lists_every_ending_with_its_trigger() -> None:
    """结局条件此前只躺在 system prompt 末尾的剧本全文里；议程能被可靠触发，
    正是因为它每轮都以独立小节出现在局面块中。这里给结局同样的待遇。

    ⚠️ 如实说：这是概率性改进。"这段剧情算不算命中结局"是纯语义判断，
    没有代码手段能确定性判定。
    """
    text = format_endings_status(SituationContext(_MODULE, None))
    assert text  # fixture 模组有结局
    for ending in _MODULE.endings:
        assert ending.id in text
        assert ending.title in text


def test_endings_status_is_empty_for_a_module_without_endings() -> None:
    """没有结局的模组 → 空串 → 整块不渲染（退化保证）。"""
    stripped = _MODULE.model_copy(update={"endings": []})
    assert format_endings_status(SituationContext(stripped, None)) == ""
