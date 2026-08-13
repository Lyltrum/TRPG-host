"""收尾是可撤回的：`ending` 阶段里玩家继续说话 = 「我们还想玩」。

## 🔴 为什么需要它（2026-08-12 真人反馈）

「我可以一直玩、一直偏离主线，但永远不会被 AI 主持人说已经结束了。」

原先自然收尾直接落 `finished`，而那是**一堵硬墙**（`agent.py` 直接返回"本局
已结束"，模型都不再跑）。收早了极贵，于是规则 10b 给 KP 加了一道「三个数都
见底才准收」的机械前提——**代码替 KP 做了它本来就该做的判断**。而玩家在原地
打转时那三个数永远不见底，落幕就永远等不到。

修法不是把阈值调准，是让**判错的代价变小**：落在 `ending`，玩家接着行动就
自动退回 `investigation`。边界画不准就不必画准了。

配套的另一半在 `capabilities/closure/`（收尾落在 ending + 停滞轮数）。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.runtime.agent import KeeperAgent
from app.core.keeper.runtime.phase import (
    PHASE_ENDING,
    PHASE_FINISHED,
    PHASE_INVESTIGATION,
    PHASE_KEY,
    load_phase,
)
from app.core.narration.contract import NarrationContext, PlayerUtterance
from app.models.room import Player, Room

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")

_db_path = Path(tempfile.mkdtemp(prefix="trpg-closure-reopen-test-")) / "reopen.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(room_code: str, phase: str) -> tuple[str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="收尾房",
            max_players=4,
            phase="InGame",
            keeper_state={PHASE_KEY: phase, "当前场景": "门厅"},
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="阿福")
        db.add(player)
        await db.flush()
        await db.commit()
        return room.id, player.id


def _agent() -> KeeperAgent:
    agent = KeeperAgent(
        api_key="fake-key",
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
    )

    async def fake_adjudicate(situation: str) -> KeeperDecision:
        return KeeperDecision(thinking="桩", narration_guidance="继续")

    async def fake_narrate_prose(*args, **kwargs) -> str:
        return "阁楼的门在你手下吱呀作响。"

    agent._adjudicate = fake_adjudicate  # ty: ignore[invalid-assignment]
    agent._narrate_prose = fake_narrate_prose  # ty: ignore[invalid-assignment]
    return agent


def _ctx(room_id: str, player_id: str, utterance: str, **kw) -> NarrationContext:
    return NarrationContext(
        utterance=utterance,
        player_nickname="阿福",
        room_id=room_id,
        player_id=player_id,
        utterances=(PlayerUtterance(player_id=player_id, nickname="阿福", text=utterance),),
        **kw,
    )


async def _phase_of(room_id: str) -> str | None:
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        return load_phase(room.keeper_state)


async def test_a_player_speaking_up_reopens_the_story() -> None:
    """「等等，我还想上阁楼」——这句话本身就是意思表示，不需要额外交互。"""
    room_id, player_id = await _seed("RPN100", PHASE_ENDING)

    outcome = await _agent().narrate(_ctx(room_id, player_id, "等等，我还想上阁楼看看"))

    assert await _phase_of(room_id) == PHASE_INVESTIGATION
    # 退回之后这一轮要**照常主持**，不是回一句"本局已结束"
    assert "本局已结束" not in outcome.text
    assert outcome.text.strip() != ""


async def test_the_heartbeat_does_not_reopen_a_closing_story() -> None:
    """心跳不是玩家的意思表示。让它退回，等于世界自己把自己的落幕撤销掉。"""
    room_id, player_id = await _seed("RPN200", PHASE_ENDING)

    ctx = _ctx(room_id, player_id, "（时间悄然流逝）", is_heartbeat=True)
    outcome = await _agent().narrate(ctx)

    assert await _phase_of(room_id) == PHASE_ENDING
    assert outcome.text == ""


async def test_a_finished_game_is_still_a_hard_wall() -> None:
    """退化保证：命中**剧本预设结局**那条路仍然直达 finished，且拒收行动。
    可撤回的只有"KP 自己觉得该停了"那一种。"""
    room_id, player_id = await _seed("RPN300", PHASE_FINISHED)

    outcome = await _agent().narrate(_ctx(room_id, player_id, "我还想上阁楼"))

    assert await _phase_of(room_id) == PHASE_FINISHED
    assert "本局已结束" in outcome.text
