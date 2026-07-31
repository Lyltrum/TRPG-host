"""per-observer 投递（exec/14 P5.2b）：分组叙事 + 只发给在场的人。

三层各测各的：
1. `ConnectionManager.send_to_players`——投递本身（假连接，不起真 WS）；
2. `KeeperAgent._narrate_per_audience`——一次裁决、按位置分段（桩掉 LLM）；
3. `ws._audience_at_speaker_location`——玩家原话该发给谁。

⚠️ "两个真实 WS 客户端各自收到不同内容"这条断言 pytest 做不了（TestClient
每条连接独立事件循环，跨循环广播挂死，已踩过三次），只能交给 e2e。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.controller.ws import _audience_at_speaker_location
from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.agent import KeeperAgent
from app.core.keeper.decision import KeeperDecision
from app.core.keeper.location_state import PLAYER_LOCATION_KEY
from app.core.keeper.module_loader import load_module
from app.core.keeper.phase import PHASE_INVESTIGATION, PHASE_KEY
from app.core.keeper.scene_state import CURRENT_NODE_KEY
from app.core.narrator import NarrationContext
from app.models.room import Player, Room
from app.service.ws_manager import ConnectionManager

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-fanout-test-")) / "fanout.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ── 1. 投递层 ───────────────────────────────────────


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


async def test_send_to_players_only_reaches_listed_players() -> None:
    manager = ConnectionManager()
    a, b, anon = _FakeSocket(), _FakeSocket(), _FakeSocket()
    manager.add("r", a, "pa")  # ty: ignore[invalid-argument-type]
    manager.add("r", b, "pb")  # ty: ignore[invalid-argument-type]
    manager.add("r", anon)  # ty: ignore[invalid-argument-type] — 未绑定身份的连接

    await manager.send_to_players("r", ["pa"], {"x": 1})
    assert a.sent == [{"x": 1}]
    assert b.sent == []
    # 没有身份的连接拿不到任何定向投递（宁可漏发，不可错发）
    assert anon.sent == []
    assert sorted(manager.connected_player_ids("r")) == ["pa", "pb"]


async def test_empty_audience_sends_to_nobody_not_everybody() -> None:
    """🔴 受众算错必须表现为"没人收到"，绝不能退化成广播（那是当场泄密）。"""
    manager = ConnectionManager()
    a = _FakeSocket()
    manager.add("r", a, "pa")  # ty: ignore[invalid-argument-type]
    await manager.send_to_players("r", [], {"x": 1})
    assert a.sent == []


async def test_remove_drops_player_binding() -> None:
    manager = ConnectionManager()
    a = _FakeSocket()
    manager.add("r", a, "pa")  # ty: ignore[invalid-argument-type]
    manager.remove("r", a)  # ty: ignore[invalid-argument-type]
    assert manager.connected_player_ids("r") == []


# ── 2. 分组叙事 ─────────────────────────────────────


def _keeper() -> KeeperAgent:
    return KeeperAgent(
        api_key="fake-key",
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
    )


async def _seed(room_code: str, keeper_state: dict) -> tuple[str, str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="分头房",
            max_players=4,
            phase="InGame",
            keeper_state={PHASE_KEY: PHASE_INVESTIGATION, **keeper_state},
        )
        db.add(room)
        await db.flush()
        a = Player(room_id=room.id, nickname="阿福")
        b = Player(room_id=room.id, nickname="阿贵")
        db.add_all([a, b])
        await db.flush()
        await db.commit()
        return room.id, a.id, b.id


def _stub(agent: KeeperAgent, decision: KeeperDecision) -> list[str]:
    suffixes: list[str] = []

    async def fake_adjudicate(situation: str) -> KeeperDecision:
        return decision

    async def fake_narrate_prose(
        situation, decision, report, issues, *, max_tokens, max_chars, extra_suffix=""
    ):
        suffixes.append(extra_suffix)
        return f"第{len(suffixes)}段叙事。"

    agent._adjudicate = fake_adjudicate  # ty: ignore[invalid-assignment]
    agent._narrate_prose = fake_narrate_prose  # ty: ignore[invalid-assignment]
    return suffixes


async def _narrate(room_code: str, *, split: bool, both_speak: bool = False):
    """播种一个两人房（可选分头），跑一轮 narrate。返回 (outcome, 各段 suffix, a, b)。"""
    agent = _keeper()
    suffixes = _stub(agent, KeeperDecision(thinking="无事", narration_guidance="继续"))
    room_id, a_id, b_id = await _seed(room_code, {CURRENT_NODE_KEY: "hall"})
    if split:
        async with _session_factory() as db:
            room = await db.get(Room, room_id)
            assert room is not None
            room.keeper_state = {**(room.keeper_state or {}), PLAYER_LOCATION_KEY: f"{b_id}@cellar"}
            await db.commit()

    context = NarrationContext(
        utterance="我看看四周",
        player_nickname="阿福",
        room_id=room_id,
        player_id=a_id,
        participant_ids=(a_id, b_id) if both_speak else (),
    )
    outcome = await agent.narrate(context)
    return outcome, suffixes, a_id, b_id


async def test_party_together_keeps_single_broadcast_and_identical_prompt() -> None:
    """退化保证：未分头 → 一段全房间叙事、**不追加任何范围提示**。"""
    outcome, suffixes, _a, _b = await _narrate("FAN001", split=False)
    assert outcome.text == "第1段叙事。"
    assert outcome.segments == []
    assert suffixes == [""]


async def test_split_party_produces_one_segment_per_acting_group() -> None:
    """阿福在门厅、阿贵在地下室，本轮只有阿福发言 → 只生成门厅那一段。"""
    outcome, suffixes, a_id, _b_id = await _narrate("FAN002", split=True)
    # 全房间正文必须为空——否则分头的两边会同时收到重复内容
    assert outcome.text == ""
    assert [s.audience for s in outcome.segments] == [(a_id,)]
    assert outcome.segments[0].node_id == "hall"
    # 阿贵那边本轮没人行动 → 不凭空生成一段
    assert len(suffixes) == 1
    assert "门厅" in suffixes[0] and "只会送达" in suffixes[0]


async def test_both_groups_act_produces_two_segments() -> None:
    """两处都有人发言 → 一次裁决、两段叙事，各自受众互不重叠。"""
    outcome, suffixes, a_id, b_id = await _narrate("FAN003", split=True, both_speak=True)
    assert outcome.text == ""
    assert [s.audience for s in outcome.segments] == [(a_id,), (b_id,)]
    assert [s.node_id for s in outcome.segments] == ["hall", "cellar"]
    assert len(suffixes) == 2
    assert "门厅" in suffixes[0] and "地下室" in suffixes[1]


async def test_non_speaking_teammate_at_same_place_still_receives() -> None:
    """同处一地但本轮没发言的人，也该收到这段——他人在现场。"""
    agent = _keeper()
    _stub(agent, KeeperDecision(thinking="无事", narration_guidance="继续"))
    # 三人房：阿福、阿贵留在门厅，阿丙独自去地下室。只有阿福发言。
    async with _session_factory() as db:
        room = Room(
            room_code="FAN004",
            room_name="三人分头房",
            max_players=4,
            phase="InGame",
            keeper_state={PHASE_KEY: PHASE_INVESTIGATION, CURRENT_NODE_KEY: "hall"},
        )
        db.add(room)
        await db.flush()
        a, b, c = (
            Player(room_id=room.id, nickname="阿福"),
            Player(room_id=room.id, nickname="阿贵"),
            Player(room_id=room.id, nickname="阿丙"),
        )
        db.add_all([a, b, c])
        await db.flush()
        room.keeper_state = {**(room.keeper_state or {}), PLAYER_LOCATION_KEY: f"{c.id}@cellar"}
        await db.commit()
        room_id, a_id, b_id = room.id, a.id, b.id

    context = NarrationContext(
        utterance="我看看四周",
        player_nickname="阿福",
        room_id=room_id,
        player_id=a_id,
    )
    outcome = await agent.narrate(context)
    assert len(outcome.segments) == 1
    assert set(outcome.segments[0].audience) == {a_id, b_id}


# ── 3. 玩家原话按位置投递 ───────────────────────────


async def test_speaker_audience_is_none_when_party_together() -> None:
    """未分头 → None → 走原来的全房间广播，行为逐字不变。"""
    room_id, a_id, _b_id = await _seed("FAN005", {CURRENT_NODE_KEY: "hall"})
    async with _session_factory() as db:
        assert await _audience_at_speaker_location(db, room_id, a_id) is None


async def test_speaker_audience_is_own_group_when_split() -> None:
    room_id, a_id, b_id = await _seed("FAN006", {CURRENT_NODE_KEY: "hall"})
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        room.keeper_state = {**(room.keeper_state or {}), PLAYER_LOCATION_KEY: f"{b_id}@cellar"}
        await db.commit()
    async with _session_factory() as db:
        assert await _audience_at_speaker_location(db, room_id, a_id) == [a_id]
        assert await _audience_at_speaker_location(db, room_id, b_id) == [b_id]
