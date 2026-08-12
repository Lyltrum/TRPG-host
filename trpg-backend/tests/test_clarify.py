"""玩家纠错通道（`exec/35`）：回滚指针 → 带澄清重裁上一轮。

🔴 为什么需要它：真人 KP 桌上最高频的交互就是「等等，我说的是绕到屋后，
不是进屋」。此前玩家唯一的手段是再说一句话、指望模型自己发现——真机多次
出现位置被写错（`exec/31 #72`、`exec/33 #79`），每次都只能靠改代码修。

这里只测 keeper 那一半（澄清有没有进裁决输入、以及**快照不归它管**）。
WS 那一半（存快照 / 回滚 / 重跑上一轮原话）在 `test_chat_ws.py`。
不跑真实 LLM：`_adjudicate` / `_narrate_prose` 用实例属性桩掉。
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
from app.core.keeper.runtime.phase import PHASE_INVESTIGATION, PHASE_KEY
from app.core.narration.contract import NarrationContext, PlayerUtterance
from app.models.room import Player, Room

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")

_db_path = Path(tempfile.mkdtemp(prefix="trpg-clarify-test-")) / "clarify.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(room_code: str, scene: str) -> tuple[str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="纠错测试房",
            max_players=4,
            phase="InGame",
            keeper_state={PHASE_KEY: PHASE_INVESTIGATION, "当前场景": scene},
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="阿福")
        db.add(player)
        await db.flush()
        await db.commit()
        return room.id, player.id


def _agent() -> tuple[KeeperAgent, dict]:
    agent = KeeperAgent(
        api_key="fake-key",
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
    )
    captured: dict = {}

    async def fake_adjudicate(situation: str) -> KeeperDecision:
        captured["situation"] = situation
        return KeeperDecision(thinking="桩", narration_guidance="继续")

    async def fake_narrate_prose(*args, **kwargs) -> str:
        return "占位叙事文本。"

    agent._adjudicate = fake_adjudicate  # ty: ignore[invalid-assignment]
    agent._narrate_prose = fake_narrate_prose  # ty: ignore[invalid-assignment]
    return agent, captured


def _ctx(room_id: str, player_id: str, utterance: str, **kw) -> NarrationContext:
    return NarrationContext(
        utterance=utterance,
        player_nickname="阿福",
        room_id=room_id,
        player_id=player_id,
        utterances=(PlayerUtterance(player_id=player_id, nickname="阿福", text=utterance),),
        **kw,
    )


async def _snapshot(room_id: str) -> dict | None:
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        return room.last_turn_snapshot if room is not None else None


async def test_the_agent_does_not_own_the_snapshot() -> None:
    """🔴 快照**不由 narrator 写**——它是"这一轮的输入"，属于编排层。

    第一版写在 `KeeperAgent` 里，于是 fallback narrator 的房间根本没有快照、
    纠错一按就报 CONFLICT。同族于「同一件事的两头，一头可插拔一头写死」：
    换一个 narrator 实现，功能就悄悄没了。

    存快照与回滚的用例在 `test_chat_ws.py`（那一层才有 submissions）。
    """
    room_id, player_id = await _seed("CLR001", "门厅")
    agent, _ = _agent()

    await agent.narrate(_ctx(room_id, player_id, "我推开书房的门"))

    assert await _snapshot(room_id) is None


async def test_the_clarification_reaches_the_adjudicator() -> None:
    """澄清必须进裁决器的输入——它是这一轮唯一变了的东西。"""
    room_id, player_id = await _seed("CLR003", "门厅")
    agent, captured = _agent()

    await agent.narrate(
        _ctx(room_id, player_id, "我绕到屋后", clarification="我说的是绕到屋后，不是进屋")
    )

    situation = captured["situation"]
    assert "我说的是绕到屋后，不是进屋" in situation
    assert "玩家纠错" in situation
    # 🔴 边界也得在场：能纠的是"你听错了我的话"，不是"我要改结果"
    assert "已经发生的事不许改写" in situation


async def test_a_normal_turn_carries_no_clarification_guidance() -> None:
    """退化保证：不纠错的那些轮，裁决输入里一个字都不该多出来。

    这条守的是「磁带不漂」——上下文组装变了就等于所有回归基线作废。
    """
    room_id, player_id = await _seed("CLR004", "门厅")
    agent, captured = _agent()

    await agent.narrate(_ctx(room_id, player_id, "我推开书房的门"))

    assert "玩家纠错" not in captured["situation"]
