"""keeper agent 六个工具的业务实现（app/core/keeper/tools.py 的 `*_impl` 层）。

测的是普通函数，不跑 LLM/SDK：自建一个独立的临时文件 SQLite（不复用
conftest 的 TestSessionLocal——conftest 明确警告不要从测试模块 import 它），
预置房间/玩家/角色卡，固定 seed 的 rng 让掷骰可断言。

module_loader 的加载与查询也在这里一并覆盖（共享同一个 fixture 剧本）。
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
from app.core.keeper import dice
from app.core.keeper.module_loader import load_module
from app.core.keeper.tools import (
    KeeperDeps,
    KeeperToolError,
    adjust_hp_impl,
    get_character_sheet_impl,
    read_module_impl,
    roll_check_impl,
    san_check_impl,
    update_state_impl,
)
from app.models.event import Event
from app.models.room import Character, Player, Room

_FIXTURE_MODULE = Path(__file__).parent / "fixtures" / "keeper_module.json"

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-test-")) / "keeper.db"
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


# ── roll_check ──────────────────────────────────────


async def test_roll_check_trained_skill(deps: KeeperDeps) -> None:
    expected_roll = random.Random(42).randint(1, 100)
    text = await roll_check_impl(deps, "侦察")
    assert f"d100={expected_roll}" in text
    assert "目标值 70" in text  # spot-hidden 总值 70
    events = await _events(deps, "keeper.check")
    assert len(events) == 1
    assert events[0].payload["target"] == 70
    # 掷骰可见性硬保证的数据源：结果必须记进 check_results（narrate 末尾由
    # 代码强制附加广播，不依赖模型把数字写进叙事）
    assert len(deps.check_results) == 1
    assert f"{expected_roll}/70" in deps.check_results[0]

    # 「侦查」同义写法必须解析到同一技能（模组文本常用写法）
    assert "目标值 70" in await roll_check_impl(deps, "侦查")


async def test_roll_check_untrained_falls_back_to_base(deps: KeeperDeps) -> None:
    # 闪避没点过 → 基础值 DEX/2 = 35
    assert "目标值 35" in await roll_check_impl(deps, "闪避")


async def test_roll_check_attribute_and_luck(deps: KeeperDeps) -> None:
    assert "目标值 60" in await roll_check_impl(deps, "力量")
    assert "目标值 55" in await roll_check_impl(deps, "幸运")


async def test_roll_check_unknown_skill(deps: KeeperDeps) -> None:
    with pytest.raises(KeeperToolError, match="未知的技能"):
        await roll_check_impl(deps, "量子力学")


async def test_roll_check_player_without_character(deps: KeeperDeps) -> None:
    with pytest.raises(KeeperToolError, match="还没有角色卡"):
        await roll_check_impl(deps, "侦察", player_name="小明")


async def test_roll_check_unknown_player_lists_roster(deps: KeeperDeps) -> None:
    with pytest.raises(KeeperToolError, match="阿福"):
        await roll_check_impl(deps, "侦察", player_name="不存在的人")


# ── get_character_sheet ─────────────────────────────


async def test_character_sheet_contents(deps: KeeperDeps) -> None:
    text = await get_character_sheet_impl(deps)
    assert "侦探福" in text and "私家侦探" in text
    assert "STR 60" in text
    # 只列真实加过点的技能，未训练的不出现
    assert "侦察 70" in text and "图书馆使用 60" in text
    assert "闪避" not in text

    # 按角色名找同一个人
    assert "侦探福" in await get_character_sheet_impl(deps, player_name="侦探福")


# ── read_module ─────────────────────────────────────


async def test_read_module_sections(deps: KeeperDeps) -> None:
    overview = read_module_impl(deps, "overview")
    assert "真相是管家做的" in overview and "受雇调查庄园失窃案" in overview

    nodes = read_module_impl(deps, "nodes")
    assert "hall" in nodes and "cellar" in nodes

    hall = read_module_impl(deps, "node:hall")
    assert "脚印" in hall and "侦查" in hall

    cellar = read_module_impl(deps, "node:cellar")
    assert "油灯" in cellar and "手套" in cellar  # 分支及嵌套分支都要可见

    safe = read_module_impl(deps, "node:hidden-safe")  # sub_node 可直接按 id 查
    assert "保险箱" in safe

    npc = read_module_impl(deps, "npc:butler")
    assert "厨房" in npc and "STR 50" in npc

    endings = read_module_impl(deps, "endings")
    assert "管家伏法" in endings


async def test_read_module_unknown_section(deps: KeeperDeps) -> None:
    with pytest.raises(KeeperToolError, match="未知的 section"):
        read_module_impl(deps, "whatever")
    with pytest.raises(KeeperToolError, match="可用节点"):
        read_module_impl(deps, "node:nope")


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


# ── adjust_hp ───────────────────────────────────────


async def test_adjust_hp_damage_and_floor(deps: KeeperDeps) -> None:
    text = await adjust_hp_impl(deps, -3, "被食尸鬼抓伤")
    assert "10 → 7" in text
    derived = await _derived(deps)
    assert derived["HP"] == 7
    assert derived["HP_MAX"] == 10  # 首次修改备份上限

    text = await adjust_hp_impl(deps, -99, "致命打击")
    assert "→ 0" in text and "倒地" in text
    assert (await _derived(deps))["HP"] == 0
    assert len(deps.check_results) == 2  # 两次 HP 变动都进了可见性记录


# ── san_check ───────────────────────────────────────


async def test_san_check_applies_loss(deps: KeeperDeps) -> None:
    seeded = random.Random(42)
    rolled = seeded.randint(1, 100)  # 与 deps.rng 同序列的第一掷
    text = await san_check_impl(deps, "0", "1d6")
    succeeded = dice.evaluate_check(rolled, 50).succeeded
    expected_loss = 0 if succeeded else seeded.randint(1, 6)

    assert f"d100={rolled}/50" in text
    derived = await _derived(deps)
    assert derived["SAN"] == 50 - expected_loss
    assert derived["SAN_MAX"] == 50
    events = await _events(deps, "keeper.san")
    assert len(events) == 1
    assert events[0].payload["loss"] == expected_loss
    assert len(deps.check_results) == 1
    assert "理智检定" in deps.check_results[0]


async def test_san_check_big_loss_warns_temporary_insanity(deps: KeeperDeps) -> None:
    # 固定损失 8（纯数字表达式），无论检定成败都 >=5 → 必须提示临时疯狂
    text = await san_check_impl(deps, "8", "8")
    assert "临时疯狂" in text
    assert (await _derived(deps))["SAN"] == 42
