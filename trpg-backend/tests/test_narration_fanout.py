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
from app.core.keeper.fact_ledger import EVENT_TYPE as FACT_EVENT_TYPE
from app.core.keeper.fact_ledger import revealed_fact_ids, visible_fact_ids
from app.core.keeper.location_state import HIDDEN_PLAYERS_KEY, PLAYER_LOCATION_KEY
from app.core.keeper.module_loader import load_module
from app.core.keeper.phase import PHASE_INVESTIGATION, PHASE_KEY
from app.core.keeper.scene_state import CURRENT_NODE_KEY
from app.core.narrator import NarrationContext, PlayerUtterance
from app.models.event import Event
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


def _capture_situations(agent: KeeperAgent, decision: KeeperDecision) -> list[str]:
    """抓每一段叙事**实际拿到的局面块**——P5.2d 守的就是它里面有什么。"""
    situations: list[str] = []

    async def fake_adjudicate(situation: str) -> KeeperDecision:
        return decision

    async def fake_narrate_prose(
        situation, decision, report, issues, *, max_tokens, max_chars, extra_suffix=""
    ):
        situations.append(situation)
        return "占位叙事。"

    agent._adjudicate = fake_adjudicate  # ty: ignore[invalid-assignment]
    agent._narrate_prose = fake_narrate_prose  # ty: ignore[invalid-assignment]
    return situations


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


async def test_party_together_keeps_single_broadcast_without_scope_hint() -> None:
    """退化保证：未分头 → 一段全房间叙事、**不追加投递范围提示**。

    （「本轮没有待掷检定，别要求掷骰」那条硬提醒与分头无关，每一轮都在，
    所以这里只断言范围提示不出现，不再断言 suffix 整体为空。）
    """
    outcome, suffixes, _a, _b = await _narrate("FAN001", split=False)
    assert outcome.text == "第1段叙事。"
    assert outcome.segments == []
    assert len(suffixes) == 1
    assert "投递范围" not in suffixes[0]


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


# ── 4. ⑥私密行动 / ②潜行 ───────────────────────────


async def test_private_flag_narrows_utterance_audience_even_when_together() -> None:
    """⑥：全队同处一地也照样只回给他自己。"""
    room_id, a_id, _b_id = await _seed("FAN007", {CURRENT_NODE_KEY: "hall"})
    async with _session_factory() as db:
        assert await _audience_at_speaker_location(db, room_id, a_id, private=True) == [a_id]


async def test_hidden_player_utterance_audience_is_self_only() -> None:
    """②：隐匿中的人做什么，同处的其他人不知道。"""
    room_id, a_id, b_id = await _seed("FAN008", {CURRENT_NODE_KEY: "hall"})
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        room.keeper_state = {**(room.keeper_state or {}), HIDDEN_PLAYERS_KEY: a_id}
        await db.commit()
    async with _session_factory() as db:
        assert await _audience_at_speaker_location(db, room_id, a_id) == [a_id]
        # 没藏的人照旧全房间广播
        assert await _audience_at_speaker_location(db, room_id, b_id) is None


async def test_private_speaker_gets_own_segment_without_splitting_party() -> None:
    """⑥：未分头 + 有私密发言者 → 走分段路径，只给他一段。"""
    agent = _keeper()
    suffixes = _stub(agent, KeeperDecision(thinking="无事", narration_guidance="继续"))
    room_id, a_id, _b_id = await _seed("FAN009", {CURRENT_NODE_KEY: "hall"})
    context = NarrationContext(
        utterance="我趁他不注意摸他口袋",
        player_nickname="阿福",
        room_id=room_id,
        player_id=a_id,
        private_player_ids=(a_id,),
    )
    outcome = await agent.narrate(context)
    assert outcome.text == ""
    assert [s.audience for s in outcome.segments] == [(a_id,)]
    assert len(suffixes) == 1
    assert "只会送达 阿福 一个人" in suffixes[0]


async def test_hidden_speaker_gets_own_segment_and_still_hears_the_room() -> None:
    """②：隐匿的阿福自己行动 → 单独一段；阿贵行动 → 阿福也在受众里（听得见）。"""
    agent = _keeper()
    suffixes = _stub(agent, KeeperDecision(thinking="无事", narration_guidance="继续"))
    room_id, a_id, b_id = await _seed("FAN010", {CURRENT_NODE_KEY: "hall"})
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        room.keeper_state = {**(room.keeper_state or {}), HIDDEN_PLAYERS_KEY: a_id}
        await db.commit()

    outcome = await agent.narrate(
        NarrationContext(
            utterance="我贴着墙根往里挪",
            player_nickname="阿福",
            room_id=room_id,
            player_id=a_id,
            participant_ids=(a_id, b_id),
        )
    )
    # 隐匿者一段（只给自己）+ 公开发言者那一组一段（受众含隐匿者本人）
    assert [s.audience for s in outcome.segments] == [(a_id,), (a_id, b_id)]
    assert "只会送达 阿福 一个人" in suffixes[0]
    assert "阿福正处于隐匿状态" in suffixes[1]


# ── 5. per-audience 上下文（P5.2d）：拿不到，才是真的说不出 ──────────


async def _seed_split_room_with_history(room_code: str):
    """两人分头 + 各自的历史：门厅的阿福、地下室的阿贵，各说过一句只有自己
    那边听得见的话，各自挣到一条只有自己知道的线索。"""
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="分头历史房",
            max_players=4,
            phase="InGame",
            keeper_state={PHASE_KEY: PHASE_INVESTIGATION, CURRENT_NODE_KEY: "hall"},
        )
        db.add(room)
        await db.flush()
        a = Player(room_id=room.id, nickname="阿福")
        b = Player(room_id=room.id, nickname="阿贵")
        db.add_all([a, b])
        await db.flush()
        room.keeper_state = {**(room.keeper_state or {}), PLAYER_LOCATION_KEY: f"{b.id}@cellar"}
        db.add_all(
            [
                Event(
                    room_id=room.id,
                    player_id=a.id,
                    event_type="action.submit",
                    payload={"utterance": "门厅这边我掀开了地毯", "audience": [a.id]},
                ),
                Event(
                    room_id=room.id,
                    player_id=b.id,
                    event_type="action.submit",
                    payload={"utterance": "地下室这边我撬开了木箱", "audience": [b.id]},
                ),
                Event(
                    room_id=room.id,
                    player_id=None,
                    event_type="action.submit",
                    payload={"utterance": "这句是分头之前说的，谁都听得见"},
                ),
                Event(
                    room_id=room.id,
                    player_id=a.id,
                    event_type=FACT_EVENT_TYPE,
                    payload={"fact_id": "f-hall", "via": "check", "audience": [a.id]},
                ),
                Event(
                    room_id=room.id,
                    player_id=b.id,
                    event_type=FACT_EVENT_TYPE,
                    payload={"fact_id": "f-cellar", "via": "check", "audience": [b.id]},
                ),
            ]
        )
        await db.commit()
        return room.id, a.id, b.id


async def test_each_segment_context_excludes_the_other_groups_history() -> None:
    """🔴 P5.2d 的核心断言：门厅那段的 prompt 里**根本没有**地下室的历史。

    这一条替代了"靠范围提示请模型别说"。变异检验：把 `visible_history` 改成
    无条件返回全部，这条立刻红。
    """
    agent = _keeper()
    situations = _capture_situations(
        agent, KeeperDecision(thinking="无事", narration_guidance="继续")
    )
    room_id, a_id, b_id = await _seed_split_room_with_history("FAN011")

    await agent.narrate(
        NarrationContext(
            utterance="阿福：我看看四周\n阿贵：我也看看",
            player_nickname="阿福",
            room_id=room_id,
            player_id=a_id,
            participant_ids=(a_id, b_id),
            utterances=(
                PlayerUtterance(player_id=a_id, nickname="阿福", text="我看看四周"),
                PlayerUtterance(player_id=b_id, nickname="阿贵", text="我也看看"),
            ),
        )
    )
    assert len(situations) == 2
    hall, cellar = situations

    # 历史：各看各的；分头之前那句两边都在
    assert "门厅这边我掀开了地毯" in hall
    assert "地下室这边我撬开了木箱" not in hall
    assert "地下室这边我撬开了木箱" in cellar
    assert "门厅这边我掀开了地毯" not in cellar
    assert "这句是分头之前说的" in hall and "这句是分头之前说的" in cellar

    # 本轮原话：门厅那段不该出现阿贵说了什么
    assert "我看看四周" in hall and "我也看看" not in hall
    assert "我也看看" in cellar and "我看看四周" not in cellar


async def test_ledger_is_scoped_to_the_audience() -> None:
    """线索账本同样按受众裁：地下室挣到的那条不进门厅那段。"""
    async with _session_factory() as db:
        assert await visible_fact_ids(db, room_id="nope", audience=frozenset()) == set()

    room_id, a_id, b_id = await _seed_split_room_with_history("FAN012")
    async with _session_factory() as db:
        assert await visible_fact_ids(db, room_id=room_id, audience=frozenset({a_id})) == {"f-hall"}
        assert await visible_fact_ids(db, room_id=room_id, audience=frozenset({b_id})) == {
            "f-cellar"
        }
        # 两个人合起来一组时，两条都不算"共同知道"——交集口径，朝保密方向失败
        assert (
            await visible_fact_ids(db, room_id=room_id, audience=frozenset({a_id, b_id})) == set()
        )
        # 守秘人视图不过滤
        assert await revealed_fact_ids(db, room_id=room_id) == {"f-hall", "f-cellar"}
