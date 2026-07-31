"""AI 玩家在系统里占一个真座位（exec/21 第一层）。

系统此前有 6 处 `is_ai` 过滤，**大多不是有意的语义，是"AI 玩家还不存在"时
顺手写的防御**。逐点裁决后 5 处改为包含、1 处保留排除。

这份测试是那张裁决表的逐格守门人。判错一格的症状跟 exec/19 #37 一样阴：
不报错，只让某个人莫名其妙收不到消息——所以每一格都要有自己的断言，
不能只测"整体跑得通"。

⚠️ 本期（第一层）AI 玩家**不说话**：它有座位、有位置、算进名单和分组，
但不提交行动、不做决策。那是第三层。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.decision import KeeperDecision
from app.core.keeper.location_state import PLAYER_LOCATION_KEY, load_player_locations
from app.core.keeper.module_loader import load_module
from app.core.keeper.phase import PHASE_INVESTIGATION, PHASE_KEY
from app.core.keeper.scene_state import CURRENT_NODE_KEY
from app.core.keeper.tools import KeeperDeps
from app.core.keeper.turn_executor import execute_side_effects
from app.models.room import Character, Player, Room

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")
_MODULE = load_module(_FIXTURE_MODULE)

_db_path = Path(tempfile.mkdtemp(prefix="trpg-ai-seat-test-")) / "seat.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


_ATTRS = {
    "STR": 55,
    "CON": 55,
    "SIZ": 55,
    "DEX": 55,
    "APP": 55,
    "INT": 55,
    "POW": 55,
    "EDU": 55,
    "LUCK": 55,
}


async def _seed(room_code: str, keeper_state: dict | None = None) -> tuple[str, str, str]:
    """一真人（阿福）+ 一 AI（阿铁），两人都有完成的角色卡。"""
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="混编房",
            max_players=4,
            phase="InGame",
            keeper_state={PHASE_KEY: PHASE_INVESTIGATION, **(keeper_state or {})},
        )
        db.add(room)
        await db.flush()
        human = Player(room_id=room.id, nickname="阿福")
        ai = Player(room_id=room.id, nickname="阿铁", is_ai=True)
        db.add_all([human, ai])
        await db.flush()
        for p in (human, ai):
            db.add(
                Character(
                    room_id=room.id,
                    player_id=p.id,
                    status="complete",
                    name=p.nickname,
                    occupation="记者",
                    age=30,
                    gender="男",
                    attributes=dict(_ATTRS),
                    derived_stats={"HP": 11, "MP": 11, "SAN": 55, "MOV": 8},
                    skills={},
                )
            )
        await db.commit()
        return room.id, human.id, ai.id


# ── 裁决 1：心跳聚光灯 —— 唯一保留排除的一格 ──────────


async def test_spotlight_ignores_the_ai_player() -> None:
    """🔴 聚光灯的职责是照顾**被冷落的真人**。

    AI 玩家不会因为没被点到而觉得无聊，把它算进来只会挤掉真人的镜头。
    这是裁决表里唯一有意保留的排除。

    ⚠️ 用例必须**把 AI 造成"最该被点到的那个"**才算真的走进被测分支：
    让真人刚说过话、AI 从没说过话（`_pick_player` 把"从没说过话"排最前）。
    第一版没造这个前置，两人都没说过话时排序碰巧仍选中真人——**变异体没被
    抓到**，正是"变异检验没抓到 ≠ 没 bug，先怀疑测试没走进被测分支"。
    """
    from app.core.keeper.heartbeat import _pick_player
    from app.models.event import Event

    room_id, human_id, ai_id = await _seed("AIS001")
    async with _session_factory() as db:
        db.add(
            Event(
                room_id=room_id,
                player_id=human_id,
                event_type="action.submit",
                payload={"utterance": "我刚说过话"},
            )
        )
        await db.commit()

    picked = await _pick_player(_session_factory, room_id, 480.0)
    assert picked is not None
    assert picked[0] == human_id, "聚光灯选中了 AI 玩家"
    assert picked[0] != ai_id


# ── 裁决 2：渲染给守秘人的名单 + 位置分组 ─────────────


async def test_ai_player_appears_in_the_roster_shown_to_the_keeper() -> None:
    """🔴 守秘人必须知道桌上有几个人。

    这份名单当初就是为了治"单人局幻觉成你们三人"。AI 在场却不在名单里，
    叙事会当它不存在——玩家看到的就是队友凭空消失。
    """
    from app.core.keeper.agent import KeeperAgent

    agent = KeeperAgent(
        api_key="fake-key",
        module=_MODULE,
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
    )
    room_id, _human_id, _ai_id = await _seed("AIS002")
    _state, _lines, roster, players = await agent._load_room_memory(room_id)

    assert any("阿铁" in line for line in roster), f"AI 不在名单里：{roster}"
    assert any("阿福" in line for line in roster)
    assert {nick for _pid, nick in players} == {"阿福", "阿铁"}


# ── 裁决 3：跟着大部队走 ────────────────────────────


async def test_ai_player_moves_with_the_party() -> None:
    """🔴 "跟你站在一起的人跟你一起走"。

    不算它，AI 会被永久留在原地，下一轮就被判成分头——正是 #37 那类 bug：
    不报错，只是那个人再也收不到大家那边的叙事。
    """
    room_id, human_id, ai_id = await _seed("AIS003", {CURRENT_NODE_KEY: "hall"})
    deps = KeeperDeps(
        room_id=room_id,
        player_id=human_id,
        session_factory=_session_factory,
        module=_MODULE,
        ruleset=build_coc7_ruleset(),
    )
    await execute_side_effects(deps, KeeperDecision(current_node_id="cellar"))

    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        locations = load_player_locations(room.keeper_state)
    assert locations.get(ai_id) == "cellar", "AI 玩家被留在了原地"
    assert locations.get(human_id) == "cellar"


# ── 裁决 4：投递受众分组 ───────────────────────────


async def test_ai_player_counts_in_the_audience_grouping() -> None:
    """AI 与真人同处一地 → 不算分头，走全房间广播（返回 None）。

    ⚠️ **分组用全量玩家、发送用连接**：`send_to_players` 按 player_id 找连接，
    AI 没有连接自然发不到——那是对的。算上它是为了让"谁跟谁在一处"算对。
    """
    from app.controller.ws import _audience_at_speaker_location

    room_id, human_id, ai_id = await _seed("AIS004", {CURRENT_NODE_KEY: "hall"})
    async with _session_factory() as db:
        assert await _audience_at_speaker_location(db, room_id, human_id) is None

    # 把 AI 单独挪到地下室 → 真的分头了，这时真人那句只发给真人
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        room.keeper_state = {**(room.keeper_state or {}), PLAYER_LOCATION_KEY: f"{ai_id}@cellar"}
        await db.commit()
    async with _session_factory() as db:
        audience = await _audience_at_speaker_location(db, room_id, human_id)
    assert audience == [human_id]


# ── 裁决 5：队友角色卡 ─────────────────────────────


async def test_ai_player_card_is_visible_to_teammates() -> None:
    """AI 是队友，它的卡照样要能被传阅。"""
    from app.service.character import list_party_characters

    room_id, human_id, ai_id = await _seed("AIS005")
    async with _session_factory() as db:
        # 鉴权走真人的重连凭证——AI 没有凭证，它是被看的那个，不是看的那个
        human = await db.get(Player, human_id)
        assert human is not None
        cards = await list_party_characters(db, room_id, human.reconnect_token)
    by_player = {c.player_id: c for c in cards}
    assert ai_id in by_player, "AI 队友的卡没有出现在传阅列表里"
    assert set(by_player) == {human_id, ai_id}
    assert by_player[ai_id].nickname == "阿铁"
    assert by_player[ai_id].status == "complete"
