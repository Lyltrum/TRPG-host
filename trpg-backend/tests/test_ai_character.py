"""AI 玩家的角色卡必须是**规则上合法**的（exec/21 第二层）。

不走建卡的 HTTP 三步（那是前端向导的形状），直接按规则算好一次落库——但规则
函数一个都不自己重写，全部复用 `coc7_rules`。

🔴 生成后跑 `validate_character` 的目的跟人类建卡时**不一样**：那次是防客户端
伪造，这次数据是我们自己生成的、没人可骗，这次是**防我们自己的生成器写出
不合法的卡**。这份测试就是那道自检的守门人。

现实教训：定性试玩脚本此前直接 PATCH 一堆技能数字进去，一个数都没过职业技能点
校验——**直接塞数据的代价不是"不安全"，是你不知道手上这张卡合不合法**，
于是拿它测出来的检定成功率也说不清。
"""

import random
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

from app.core.coc7.content import build_coc7_ruleset
from app.core.coc7.rules import (
    GENERATION_POINT_BUY,
    NON_ALLOCATABLE_SKILL_IDS,
    SKILL_CAP,
    validate_character_with_occupation,
)
from app.core.db import Base
from app.main import app
from app.models.room import Character, Player, Room
from app.service.ai_player import (
    _allocate_attributes,
    _allocate_skills,
    count_ai_players,
    create_ai_player,
)

_RULESET = build_coc7_ruleset()

_db_path = Path(tempfile.mkdtemp(prefix="trpg-ai-char-test-")) / "aichar.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ── 生成器本身：多种子扫一遍 ─────────────────────────


@pytest.mark.parametrize("seed", range(20))
def test_generated_card_is_always_rule_valid(seed: int) -> None:
    """🔴 这条是第二层的核心断言：**每个种子、每个职业都要产出合法的卡**。

    只测一次"碰巧合法"没有意义——生成器的 bug 往往只在某些职业上现形
    （技能点公式不同、自选槽结构不同、信用区间不同）。
    """
    rng = random.Random(seed)
    attributes = _allocate_attributes(_RULESET, rng)
    occupation = rng.choice(_RULESET.occupations)
    skills = _allocate_skills(_RULESET, occupation, attributes, rng)

    issues = validate_character_with_occupation(
        _RULESET,
        attributes=attributes,
        occupation=occupation,
        skills=skills,
        generation_method=GENERATION_POINT_BUY,
    )
    assert not issues, f"职业 {occupation.name} 种子 {seed}：{[i.code for i in issues]}"


def test_every_occupation_can_be_generated() -> None:
    """全部职业各来一张——职业表有 229 项，抽样测不到的那些正是容易出问题的。"""
    bad: list[str] = []
    for i, occupation in enumerate(_RULESET.occupations):
        rng = random.Random(i)
        attributes = _allocate_attributes(_RULESET, rng)
        skills = _allocate_skills(_RULESET, occupation, attributes, rng)
        if validate_character_with_occupation(
            _RULESET,
            attributes=attributes,
            occupation=occupation,
            skills=skills,
            generation_method=GENERATION_POINT_BUY,
        ):
            bad.append(occupation.name)
    assert not bad, f"这些职业造不出合法的卡：{bad[:10]}（共 {len(bad)} 个）"


def test_attributes_respect_the_point_buy_budget() -> None:
    """点数购买法：参与购买的属性总和不超预算，每项落在区间内。

    幸运不参与购买（只能掷），单独排除。
    """
    point_buy = _RULESET.attribute_point_buy
    assert point_buy is not None
    for seed in range(10):
        attributes = _allocate_attributes(_RULESET, random.Random(seed))
        pool = {k: v for k, v in attributes.items() if k != "LUCK"}
        assert sum(pool.values()) <= point_buy.budget
        for key, value in pool.items():
            assert point_buy.min_value <= value <= point_buy.max_value, f"{key}={value}"


def test_never_allocates_forbidden_skills() -> None:
    """克苏鲁神话建卡阶段不能加点（规则明文禁止）。"""
    for seed in range(10):
        rng = random.Random(seed)
        attributes = _allocate_attributes(_RULESET, rng)
        occupation = rng.choice(_RULESET.occupations)
        skills = _allocate_skills(_RULESET, occupation, attributes, rng)
        assert not (set(skills) & NON_ALLOCATABLE_SKILL_IDS)
        assert all(v <= SKILL_CAP for v in skills.values())


# ── 落库 ───────────────────────────────────────────


async def _room(room_code: str) -> str:
    async with _session_factory() as db:
        room = Room(room_code=room_code, room_name="AI 房", max_players=4, phase="Building")
        db.add(room)
        await db.commit()
        return room.id


async def test_created_ai_player_is_seated_with_a_complete_card() -> None:
    room_id = await _room("AIC001")
    async with _session_factory() as db:
        player = await create_ai_player(db, room_id, nickname="阿铁", seed=1)

    async with _session_factory() as db:
        saved = await db.get(Player, player.id)
        assert saved is not None
        assert saved.is_ai is True
        # 开局条件是"所有人建卡完成"——AI 的卡一落库就是完成态，天然满足
        assert saved.has_character is True
        # 🔴 另一个开局条件是"非房主全员已就绪"。AI 没有连接、点不了那个按钮，
        # 留 False 会让房主的「开始游戏」永久点不亮。
        assert saved.ready is True
        character = (
            await db.execute(select(Character).where(Character.player_id == player.id))
        ).scalar_one()
    assert character.status == "complete"
    assert character.occupation
    assert (character.derived_stats or {}).get("HP")
    # 30 岁无年龄修正 → 分配值与有效值相同，两份都要在（否则 P5 的两份属性
    # 记账在 AI 身上就是空的）
    assert character.allocated_attributes == character.attributes


async def test_same_seed_produces_the_same_card() -> None:
    """可复现：测试与试玩装置要能造出同一张卡，否则实测数据不可比。"""
    room_a, room_b = await _room("AIC002"), await _room("AIC003")
    async with _session_factory() as db:
        pa = await create_ai_player(db, room_a, nickname="阿铁", seed=42)
    async with _session_factory() as db:
        pb = await create_ai_player(db, room_b, nickname="阿铁", seed=42)
    async with _session_factory() as db:
        ca = (await db.execute(select(Character).where(Character.player_id == pa.id))).scalar_one()
        cb = (await db.execute(select(Character).where(Character.player_id == pb.id))).scalar_one()
    assert ca.attributes == cb.attributes
    assert ca.skills == cb.skills
    assert ca.occupation == cb.occupation


async def test_named_occupation_is_honored_and_unknown_one_is_refused() -> None:
    room_id = await _room("AIC004")
    async with _session_factory() as db:
        player = await create_ai_player(db, room_id, nickname="阿铁", occupation_name="私家侦探")
        character = (
            await db.execute(select(Character).where(Character.player_id == player.id))
        ).scalar_one()
    assert character.occupation == "私家侦探"

    async with _session_factory() as db:
        with pytest.raises(ValueError, match="没有这个职业"):
            await create_ai_player(db, room_id, nickname="阿钢", occupation_name="星际战士")


async def test_count_ai_players() -> None:
    room_id = await _room("AIC005")
    async with _session_factory() as db:
        assert await count_ai_players(db, room_id) == 0
        await create_ai_player(db, room_id, nickname="阿铁", seed=1)
        await create_ai_player(db, room_id, nickname="阿铜", seed=2)
        assert await count_ai_players(db, room_id) == 2


# ── API 端点 ───────────────────────────────────────

_ROOMS = "/api/v1/rooms"


@pytest.fixture
def sync_client() -> TestClient:
    return TestClient(app)


def _register(client: TestClient, account: str) -> str:
    r = client.post(
        "/api/v1/auth/register",
        json={"account": account, "password": "secret1", "nickname": "房主"},
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["token"]


def _create_room(client: TestClient, token: str, max_players: int = 4) -> dict:
    r = client.post(
        _ROOMS,
        json={"roomName": "AI 队友测试房", "nickname": "房主", "maxPlayers": max_players},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_add_ai_player_endpoint(sync_client) -> None:
    """房主加 AI 队友：201 + 成员列表里多一个 isAi 的人，且它已完成建卡。

    `has_character=True` 是关键——开局条件是"全员建卡完成"，AI 的卡一落库
    就是完成态，所以加了 AI 不会卡住开局。
    """
    token = _register(sync_client, "ai_host")
    room = _create_room(sync_client, token)
    rh = {"X-Reconnect-Token": room["reconnectToken"]}

    r = sync_client.post(f"/api/v1/rooms/{room['roomId']}/ai-players", json={"seed": 7}, headers=rh)
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["isAi"] is True
    assert data["hasCharacter"] is True

    preview = sync_client.get(f"/api/v1/rooms/{room['roomCode']}").json()["data"]
    assert preview["playerCount"] == 2
    assert [p["isAi"] for p in preview["players"]] == [False, True]


def test_add_ai_player_requires_host(sync_client) -> None:
    """非房主不能加 AI——凭证不对就是 403，不能靠"反正没人会调"过关。"""
    token = _register(sync_client, "ai_host2")
    room = _create_room(sync_client, token)
    r = sync_client.post(
        f"/api/v1/rooms/{room['roomId']}/ai-players",
        json={},
        headers={"X-Reconnect-Token": "not-a-real-token"},
    )
    assert r.status_code == 403


def test_add_ai_player_respects_max_players(sync_client) -> None:
    """房间满了就不能再加——AI 占的是真座位，不是额外附加的。"""
    token = _register(sync_client, "ai_host3")
    room = _create_room(sync_client, token, max_players=2)
    rh = {"X-Reconnect-Token": room["reconnectToken"]}

    assert (
        sync_client.post(
            f"/api/v1/rooms/{room['roomId']}/ai-players", json={}, headers=rh
        ).status_code
        == 201
    )
    full = sync_client.post(f"/api/v1/rooms/{room['roomId']}/ai-players", json={}, headers=rh)
    assert full.status_code == 409
