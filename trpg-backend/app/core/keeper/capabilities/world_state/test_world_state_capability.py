"""world_state 能力的验收：自由文本世界状态的记账。

覆盖三件事：
1. 并发写不丢键（JSON 列整体重新赋值，读-改-写必须串行）；
2. 主体收口（exec/24 §8.2）——subject 必须解析成剧本里的 id，未知一律拒绝；
3. 键收进白名单（`exec/40` ④）——自由键写不进去，拒绝时必须给出走得通的修法。
"""

import asyncio
import random
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys
from app.core.keeper.capabilities.world_state.executor import update_state_impl
from app.core.keeper.capabilities.world_state.game_time import (
    GAME_TIME_KEY,
    goes_backwards,
    parse_game_time,
)
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.runtime.deps import KeeperDeps, KeeperToolError
from app.models.event import Event
from app.models.room import Character, Player, Room

# 模组夹具几片能力共用，仍集中放在 tests/fixtures
#: 🔴 用锚点找，不数层数：`exec/27` 阶段 5 挪目录时 `catalog.py` 的
#: `parents[3]` 当场指错一层，症状只是一条用例**静默 skip**（全套照样绿）。
_TESTS_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "trpg-backend") / "tests"
_FIXTURE_MODULE = _TESTS_DIR / "fixtures" / "keeper_module.json"

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
    全部存活——去掉锁这个测试会红（lost update）。

    键换成白名单内的（`exec/40` ④ 收口之后自由键写不进去了）——这条测的是
    **并发写不丢键**，用哪几个键不是它的命题。
    """
    await asyncio.gather(
        update_state_impl(deps, "当前场景", "门厅"),
        update_state_impl(deps, "游戏内时间", "第1天 傍晚"),
        update_state_impl(deps, "态度", "警觉", "butler-public"),
    )
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert room.keeper_state == {
            "当前场景": "门厅",
            "游戏内时间": "第1天 傍晚",
            "butler-public.态度": "警觉",
        }


async def test_update_state_merges_and_persists(deps: KeeperDeps) -> None:
    await update_state_impl(deps, "当前场景", "门厅")
    await update_state_impl(deps, "态度", "警觉", "butler-public")
    await update_state_impl(deps, "当前场景", "地下室")  # 覆盖同名旧值

    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert room.keeper_state == {"当前场景": "地下室", "butler-public.态度": "警觉"}
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
    line, issue = await update_state_impl(deps, GAME_TIME_KEY, "第2天 夜晚", "world")
    assert issue is None
    assert "游戏内时间 = 第2天 夜晚" in line
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert room.keeper_state == {"游戏内时间": "第2天 夜晚"}


def test_parsing_game_time() -> None:
    assert parse_game_time("第2天 夜晚") == (2, 7)
    assert parse_game_time("第10天 清晨") == (10, 2)
    # 只有一半也算数：模组里「第3天」「夜里」都单独出现过
    assert parse_game_time("第3天") == (3, 0)
    assert parse_game_time("傍晚时分") == (0, 6)
    # 完全认不出来 → None（放行，不是报错）
    assert parse_game_time("案发之后的某个时候") is None
    assert parse_game_time("") is None
    assert parse_game_time(None) is None


def test_synonyms_of_the_same_period_do_not_count_as_going_backwards() -> None:
    """🔴 「傍晚」「黄昏」分不出先后，就别硬分——分不出来的宁可判成"没变"，
    也不要判成"倒流"然后把一次合法的写入拒掉。"""
    assert not goes_backwards("第2天 傍晚", "第2天 黄昏")
    assert not goes_backwards("第2天 晚上", "第2天 夜晚")
    # 认不出来的那一边一律放行
    assert not goes_backwards("案发之后", "第1天 清晨")
    assert not goes_backwards("第5天 深夜", "过了很久")


async def test_game_time_cannot_run_backwards(deps: KeeperDeps) -> None:
    """🔴 2026-08-14：时间此前是纯写给模型看的字符串，**没有任何代码路径会因为
    它写错而出问题**——所以实测一整局只更新过一次也没人知道。倒流是代码判得了
    的记账错误，直接拒。"""
    await update_state_impl(deps, GAME_TIME_KEY, "第2天 清晨", "world")
    with pytest.raises(KeeperToolError, match="不能倒流"):
        await update_state_impl(deps, GAME_TIME_KEY, "第1天 夜晚", "world")
    # 同一天里往回退也拒
    with pytest.raises(KeeperToolError, match="不能倒流"):
        await update_state_impl(deps, GAME_TIME_KEY, "第2天 凌晨", "world")

    # 往前推、原地不动都放行
    await update_state_impl(deps, GAME_TIME_KEY, "第2天 下午", "world")
    await update_state_impl(deps, GAME_TIME_KEY, "第2天 下午", "world")
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert (room.keeper_state or {})[GAME_TIME_KEY] == "第2天 下午"


async def test_an_unparseable_time_is_let_through(deps: KeeperDeps) -> None:
    """认不出格式的一律放行——不认识的写法不该变成"你不许写"。"""
    await update_state_impl(deps, GAME_TIME_KEY, "第2天 夜晚", "world")
    await update_state_impl(deps, GAME_TIME_KEY, "案发之后的某个时候", "world")
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert (room.keeper_state or {})[GAME_TIME_KEY] == "案发之后的某个时候"


async def test_unknown_subject_is_refused(deps: KeeperDeps) -> None:
    """未知 id 一律拒绝——与 NPC/节点/议程/密级的处理一致。白名单外的东西
    不进状态，否则又回到"自由文本当标识符"。"""
    with pytest.raises(KeeperToolError, match="未知的状态主体"):
        await update_state_impl(deps, "态度", "警觉", "不存在的人")


async def test_entity_name_hidden_in_a_world_key_is_now_blocked(deps: KeeperDeps) -> None:
    """🔴 `管家态度` 这种把主体塞进 key 的写法：**2026-08-16 起硬拦**。

    这条原来叫 `..._is_recorded_not_blocked`，断言的是"记 issue 但照样写进去"，
    理由是「判不准该不该拦时报而不断」。它的 docstring 当时就写着**"将来要
    硬化时有据可依"**——现在数据有了：全库扫描出模型自己发明的 29 种键，
    其中一半是在给代码已有的账本造影子（`阿贵位置`／`已获线索`／`已购物品`）。

    而且这里本来就不属于"判不准"：键在不在白名单里是个确定的判断，不是语义
    猜测。**报而不断适用于判不准的那一半，不适用于判得准的这一半。**

    修法是走得通的（这是加门的前提）：改成 `subject=butler-public, key=态度`。
    """
    with pytest.raises(KeeperToolError) as exc:
        await update_state_impl(deps, "管家态度", "警觉", "world")
    assert "new_threads" in str(exc.value), "拒绝时必须说清楚该往哪儿写"

    # 挂对主体的写法照常可用
    line, _issue = await update_state_impl(deps, "态度", "警觉", "butler-public")
    assert "警觉" in line


# ── 键收进白名单（exec/40 ④，2026-08-16）──────────────


async def test_invented_world_keys_are_refused_with_a_way_out(deps: KeeperDeps) -> None:
    """🔴 模型自己发明的世界级键一律拒绝，**但必须给出走得通的修法**。

    这几个键名不是编的——是全库扫描出来的真实样本（29 种发明键里的一部分）。
    """
    for key in ("委托进度", "包裹状态", "钥匙", "金属盒内容", "侦察大失败后果"):
        with pytest.raises(KeeperToolError) as exc:
            await update_state_impl(deps, key, "随便什么值", "world")
        message = str(exc.value)
        assert "new_threads" in message, f"{key} 的拒绝没告诉模型该往哪儿写"


async def test_shadow_ledgers_are_refused(deps: KeeperDeps) -> None:
    """🔴 最该拦的一类：给代码已有账本造第二份。

    `阿贵位置` 旁边就是代码的 `玩家位置`、`已获线索` 旁边就是事实账本 L1、
    `已购物品` 旁边就是 inventory。而代码那几份对模型是**不可见的**
    （`visible_keeper_state` 滤掉保留键），所以它只能自己记一份——两份账
    谁也不认识谁，而它那份永远不会被任何代码路径清掉。
    """
    # 🔴 这几个**不是**保留键，所以在收口之前一路畅通无阻地写了进去
    for key in ("阿贵位置", "已获线索", "已购物品"):
        assert key not in deps.reserved_state_keys, f"{key} 要是保留键，这条用例就白写了"
        with pytest.raises(KeeperToolError) as exc:
            await update_state_impl(deps, key, "随便什么值", "world")
        assert "局面块" in str(exc.value), f"{key} 应该被告知系统已经记了这一份"

    # 对照：`在场NPC` 本来就是保留键，走更早那条更具体的拒绝路径
    with pytest.raises(KeeperToolError, match="由系统记账"):
        await update_state_impl(deps, "在场NPC", "科比特", "world")


async def test_entity_keys_are_narrowed_too(deps: KeeperDeps) -> None:
    """🔴 实体级的键同样要收。

    `subject` 有 id 只解决了"挂在谁身上"，`key` 仍是自由文本——实测同一个 NPC
    身上并存过 `态度`／`对lmh的态度`／`对张家豪的态度` 三种写法。
    """
    with pytest.raises(KeeperToolError):
        await update_state_impl(deps, "对张家豪的态度", "警觉", "butler-public")
    # 白名单内的照常
    line, _ = await update_state_impl(deps, "态度", "警觉", "butler-public")
    assert "butler-public.态度" in line


async def test_the_two_world_keys_still_work(deps: KeeperDeps) -> None:
    """退化证明：收口不许把正常记账一起拦掉。"""
    for key, value in (("当前场景", "地下室"), ("游戏内时间", "第2天 夜晚")):
        line, issue = await update_state_impl(deps, key, value, "world")
        assert issue is None
        assert value in line
