"""world_state 能力的验收：自由文本世界状态的记账。

覆盖三件事：
1. 并发写不丢键（JSON 列整体重新赋值，读-改-写必须串行）；
2. 主体收口（exec/24 §8.2）——subject 必须解析成剧本里的 id，未知一律拒绝；
3. 世界级键里混进实体名时**记 issue 不阻断**（阻断会把守秘人想记的整条丢掉）。
"""

import asyncio
import random
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys
from app.core.keeper.capabilities.world_state.executor import update_state_impl
from app.core.keeper.deps import KeeperDeps, KeeperToolError
from app.core.keeper.module_loader import load_module
from app.models.event import Event
from app.models.room import Character, Player, Room

# 模组夹具几片能力共用，仍集中放在 tests/fixtures
_FIXTURE_MODULE = Path(__file__).resolve().parents[5] / "tests" / "fixtures" / "keeper_module.json"

_db_path = Path(tempfile.mkdtemp(prefix="trpg-worldstate-test-")) / "keeper.db"
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
    """预置一个房间 + 两名玩家（发起者「阿福」带角色卡，「小明」没有卡）。"""
    async with _session_factory() as db:
        room = Room(room_code="KEEP01", room_name="测试房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        actor = Player(room_id=room.id, nickname="阿福")
        other = Player(room_id=room.id, nickname="小明")
        db.add_all([actor, other])
        await db.flush()
        db.add(
            Character(
                room_id=room.id,
                player_id=actor.id,
                status="complete",
                name="侦探福",
                occupation="私家侦探",
                age=32,
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
                skills={"spot-hidden": 70, "library-use": 60},
            )
        )
        await db.commit()
        room_id, actor_id = room.id, actor.id

    return KeeperDeps(
        room_id=room_id,
        player_id=actor_id,
        session_factory=_session_factory,
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
        rng=random.Random(42),
    )


async def _events(deps: KeeperDeps, event_type: str) -> list[Event]:
    async with _session_factory() as db:
        result = await db.execute(select(Event).where(Event.event_type == event_type))
        return list(result.scalars())


async def _character(deps: KeeperDeps) -> Character:
    async with _session_factory() as db:
        result = await db.execute(select(Character).where(Character.room_id == deps.room_id))
        return result.scalars().one()


async def _derived(deps: KeeperDeps) -> dict:
    """角色卡当前衍生值。fixture 必定写入了 derived_stats，断言帮类型检查收窄。"""
    derived = (await _character(deps)).derived_stats
    assert derived is not None
    return derived


# ── update_state ────────────────────────────────────


async def test_update_state_concurrent_calls_keep_all_keys(deps: KeeperDeps) -> None:
    """🔴 SDK 会并行执行同一轮的多个工具调用（真实 DeepSeek 冒烟实测：一轮里
    三次 update_state 只留下最后一个键）。write_lock 必须让三个并发调用的键
    全部存活——去掉锁这个测试会红（lost update）。"""
    await asyncio.gather(
        update_state_impl(deps, "场景", "门厅"),
        update_state_impl(deps, "线索", "脚印"),
        update_state_impl(deps, "时间", "傍晚"),
    )
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert room.keeper_state == {"场景": "门厅", "线索": "脚印", "时间": "傍晚"}


async def test_update_state_merges_and_persists(deps: KeeperDeps) -> None:
    await update_state_impl(deps, "当前场景", "门厅")
    await update_state_impl(deps, "已获线索", "脚印")
    await update_state_impl(deps, "当前场景", "地下室")  # 覆盖同名旧值

    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert room.keeper_state == {"当前场景": "地下室", "已获线索": "脚印"}
    assert len(await _events(deps, "keeper.state")) == 3


# HP 变更的用例跟着 health 能力走了（exec/27 阶段 2）：
# `app/core/keeper/capabilities/health/test_health_capability.py`


# ── 状态主体收口（exec/24 §8.2）────────────────────


async def test_state_hangs_on_an_entity_id(deps: KeeperDeps) -> None:
    """挂了主体的状态，键的形状是 `<主体 id>.<属性>`。

    这条是长战役的地基：**没有主体的状态既不可按位置/章节裁剪，也回答不了
    「谁看得见」**（exec/24 §8.2）。顺带治掉"同一件事换个措辞变两条"。
    """
    line, issue = await update_state_impl(deps, "态度", "警觉", "butler-public")
    assert issue is None
    assert "butler-public.态度" in line

    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert room.keeper_state == {"butler-public.态度": "警觉"}


async def test_state_subject_accepts_a_name_and_normalizes_to_id(deps: KeeperDeps) -> None:
    """写名字也认，但**存进去的是 id**——同一个 NPC 换个叫法不会变成两条。"""
    await update_state_impl(deps, "态度", "恐惧", "管家")
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert list((room.keeper_state or {}).keys()) == ["butler-public.态度"]


async def test_state_subject_accepts_a_node(deps: KeeperDeps) -> None:
    await update_state_impl(deps, "状态", "门被撞坏了", "hall")
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert "hall.状态" in (room.keeper_state or {})


async def test_world_level_state_keeps_a_bare_key(deps: KeeperDeps) -> None:
    """不属于任何实体的（时间/天气/委托进度）仍然是裸键，不强行套一个假主体。"""
    line, issue = await update_state_impl(deps, "游戏内时间", "第2天 夜晚", "world")
    assert issue is None
    assert "游戏内时间 = 第2天 夜晚" in line
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert room.keeper_state == {"游戏内时间": "第2天 夜晚"}


async def test_unknown_subject_is_refused(deps: KeeperDeps) -> None:
    """未知 id 一律拒绝——与 NPC/节点/议程/密级的处理一致。白名单外的东西
    不进状态，否则又回到"自由文本当标识符"。"""
    with pytest.raises(KeeperToolError, match="未知的状态主体"):
        await update_state_impl(deps, "态度", "警觉", "不存在的人")


async def test_entity_name_hidden_in_a_world_key_is_recorded_not_blocked(
    deps: KeeperDeps,
) -> None:
    """🔴 `管家态度` 这种把主体塞进 key 的写法：**记 issue，不阻断**。

    阻断会把守秘人想记的东西整条丢掉，而它可能只是措辞习惯。留痕让"还有多少
    条没挂对主体"变成可统计的量，将来要硬化时有据可依。
    """
    line, issue = await update_state_impl(deps, "管家态度", "警觉", "world")
    assert issue is not None
    assert "butler-public" in issue
    assert "管家态度 = 警觉" in line  # 照样写进去了
