"""玩家在问守秘人，不是在角色内行动（exec/19 #40）。

真人实测 2026-07-31：玩家打字「科比特先生在家吗」，问的是守秘人（他忘了这个
设定），叙事却把它演成角色在门厅里喊话——「凌铭辉的声音在门厅里消散，无人
应答」——并且照常写了场景指针。根因是 `player_state` 这条轴上没有"玩家在问
KP"这一类，它只能落进 `normal`，于是走了普通推进路径。

修法是加 `question_to_kp`，命中时**代码强制**把推进世界的手段全部收走
（检定/SAN/移动/场景指针），只留"回答"。分类本身由裁决器判（歧义只能靠语义，
与 #8 迷茫检测同一先例），这里验证的是分类命中后的代码行为。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.agent import KeeperAgent
from app.core.keeper.decision import CheckRequest, KeeperDecision, PlayerMove
from app.core.keeper.module_loader import load_module
from app.core.keeper.phase import PHASE_INVESTIGATION, PHASE_KEY
from app.core.keeper.scene_state import CURRENT_NODE_KEY
from app.core.narrator import NarrationContext
from app.models.room import Character, Player, Room

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-kpq-test-")) / "kpq.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _keeper() -> KeeperAgent:
    return KeeperAgent(
        api_key="fake-key",
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
    )


async def _seed(room_code: str) -> tuple[str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="提问房",
            max_players=4,
            phase="InGame",
            keeper_state={PHASE_KEY: PHASE_INVESTIGATION, CURRENT_NODE_KEY: "hall"},
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="凌铭辉")
        db.add(player)
        await db.flush()
        db.add(
            Character(
                room_id=room.id,
                player_id=player.id,
                status="complete",
                name="凌铭辉",
                occupation="记者",
                age=30,
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
                },  # fmt: skip
                derived_stats={"HP": 10, "MP": 10, "SAN": 50, "MOV": 8},
                skills={"spot_hidden": 40},
            )
        )
        await db.commit()
        return room.id, player.id


async def _run(room_code: str, player_state: str):
    """裁决器给出一个"想推进世界"的裁决，只有 player_state 这一项不同。"""
    agent = _keeper()
    captured: dict = {}

    async def fake_adjudicate(situation: str) -> KeeperDecision:
        return KeeperDecision(
            thinking="玩家发问",
            narration_guidance="裁决给出的原始指引",
            player_state=player_state,  # ty: ignore[invalid-argument-type]
            checks=[CheckRequest(skill_id="spot-hidden", reason="环顾门厅")],
            current_node_id="cellar",
            moves=[PlayerMove(player="凌铭辉", node_id="cellar")],
        )

    async def fake_narrate_prose(
        situation, decision, report, issues, *, max_tokens, max_chars, extra_suffix=""
    ):
        captured["guidance"] = decision.narration_guidance
        return "占位叙事文本。"

    agent._adjudicate = fake_adjudicate  # ty: ignore[invalid-assignment]
    agent._narrate_prose = fake_narrate_prose  # ty: ignore[invalid-assignment]

    room_id, player_id = await _seed(room_code)
    outcome = await agent.narrate(
        NarrationContext(
            utterance="科比特先生在家吗",
            player_nickname="凌铭辉",
            room_id=room_id,
            player_id=player_id,
        )
    )
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        node = (room.keeper_state or {}).get(CURRENT_NODE_KEY)
    return outcome, captured, node


async def test_question_to_kp_takes_away_every_way_to_advance_the_world() -> None:
    outcome, captured, node = await _run("KPQ001", "question_to_kp")
    # 提问不发检定
    assert outcome.check_requests == []
    # 提问不挪任何人：场景指针停在原地
    assert node == "hall"
    # 叙事拿到的是"玩家在问你"的指引，且原始指引没被吞掉
    assert "玩家在问你" in captured["guidance"]
    assert "不要把它演成角色的动作或喊话" in captured["guidance"]
    assert "裁决给出的原始指引" in captured["guidance"]


async def test_same_decision_as_normal_still_advances() -> None:
    """对照组：一模一样的裁决，只是分类是 normal → 检定照发、场景照推。

    没有这一条，上面那个测试即使把整条分支删掉也可能碰巧绿。
    """
    outcome, captured, node = await _run("KPQ002", "normal")
    assert [c.skill for c in outcome.check_requests] == ["侦察"]
    assert node == "cellar"
    assert "玩家在问你" not in captured["guidance"]
