"""模组列表按主人过滤（`exec/29` 第 5 步）。

## 🔴 这是导入功能带出来的一个既有缺陷

`list_modules` 原本是 `select(Scenario)`，返回全部。在导入落地之前那是对的——
表里只有随发版进来的内置模组，本来就该人人可见。**导入落地的那一刻它就变成
「每个人都能看到别人导入的模组」**，连第三方模组的标题都露出去了。

同族判据：**放开一个约束前，先找谁在依赖它。** 这里依赖的是"scenarios 表里
只有无主行"这个当时成立、现在不成立的前提；跟 `progression/executor` 那个
"endings 一定非空"的静默兜底是同一类。

## 未登录看到的是**更少**不是更多

`user_id=None` 只返回内置模组。未登录退化成"看到全部"是最坏的一种默认值。
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import Base
from app.core.errors import AppException, ErrorCode
from app.models.content import Game, GameSystem, Scenario
from app.models.replay import ModuleImportJob
from app.models.room import Player, Room
from app.models.user import User
from app.service.room import delete_module, get_module_detail, list_modules

_db_path = Path(tempfile.mkdtemp(prefix="trpg-module-own-test-")) / "own.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture
async def world():
    """一个内置模组 + 两个用户各自导入一个。"""
    async with _session_factory() as db:
        game = Game(name="跑团")
        db.add(game)
        await db.flush()
        system = GameSystem(game_id=game.id, name="COC7")
        db.add(system)
        await db.flush()

        alice = User(account="alice", password_hash="x", nickname="阿福")
        bob = User(account="bob", password_hash="x", nickname="阿贵")
        db.add_all([alice, bob])
        await db.flush()

        db.add_all(
            [
                Scenario(
                    id=str(uuid.uuid4()),
                    game_system_id=system.id,
                    title="内置模组",
                    owner_user_id=None,
                ),
                Scenario(
                    id=str(uuid.uuid4()),
                    game_system_id=system.id,
                    title="阿福导入的",
                    owner_user_id=alice.id,
                ),
                Scenario(
                    id=str(uuid.uuid4()),
                    game_system_id=system.id,
                    title="阿贵导入的",
                    owner_user_id=bob.id,
                ),
            ]
        )
        await db.commit()
        yield alice.id, bob.id


@pytest.fixture
async def ids():
    """三份模组的 id，按标题取。"""
    async with _session_factory() as db:
        rows = await db.execute(select(Scenario.title, Scenario.id))
        return dict(rows.tuples().all())


async def test_i_do_not_see_other_peoples_imports(world) -> None:
    """🔴 核心回归：别人导入的模组连标题都不该出现在我的列表里。

    变异检验：把 service 里那个 `or_(...)` 换回 `select(Scenario)`，这条当场红。
    """
    alice_id, _bob_id = world

    async with _session_factory() as db:
        titles = {m.title for m in await list_modules(db, user_id=alice_id)}

    assert titles == {"内置模组", "阿福导入的"}
    assert "阿贵导入的" not in titles


async def test_anonymous_sees_only_builtins_not_everything(world) -> None:
    """🔴 没登录看到的是**更少**，不是更多。

    "拿不到身份就放行全部"是这个项目反复强调不许有的那种静默兜底。
    """
    async with _session_factory() as db:
        titles = {m.title for m in await list_modules(db)}

    assert titles == {"内置模组"}


async def test_imported_flag_is_derived_not_stored(world) -> None:
    """`is_imported` 由 `owner_user_id` 推出，不是另一份状态——
    一份数据扮演两个角色必出结构性 bug。"""
    alice_id, _ = world

    async with _session_factory() as db:
        modules = {m.title: m for m in await list_modules(db, user_id=alice_id)}

    assert modules["内置模组"].is_imported is False
    assert modules["阿福导入的"].is_imported is True
    assert modules["阿福导入的"].created_at is not None, "导入的模组要能显示导入日期"


def test_import_list_route_is_declared_before_the_module_detail_route() -> None:
    """🔴 `/modules/import` 必须声明在 `/modules/{module_id}` 之前。

    FastAPI 按声明顺序匹配——写在后面会被当成 `module_id="import"` 吞掉，
    症状是"我的导入记录"接口莫名其妙返回 404 模组不存在。
    """
    from app.controller.v1.modules import router

    paths = [r.path for r in router.routes]  # ty: ignore[unresolved-attribute]

    assert paths.index("/modules/import") < paths.index("/modules/{module_id}")


# ── 详情端点的受众（2026-08-19 补；此前它没有任何鉴权）────────────


async def test_a_builtin_module_is_readable_by_anyone_logged_in(world, ids) -> None:
    """内置模组是目录，登录了就看得见——这条是"别把门开过头"的对照组。"""
    _alice, bob = world
    async with _session_factory() as db:
        assert await get_module_detail(db, ids["内置模组"], bob) is not None


async def test_i_can_read_my_own_import(world, ids) -> None:
    alice, _bob = world
    async with _session_factory() as db:
        assert await get_module_detail(db, ids["阿福导入的"], alice) is not None


async def test_someone_elses_import_reads_as_not_existing(world, ids) -> None:
    """🔴 **这条是这次改动的全部理由**：`list_modules` 早就按主人过滤了，
    详情这一头**一直是完全公开的**（连登录都不需要）。一份数据两个出口，
    规则只落了一处。

    口径跟 `5cfbe6c` 给「拿它开新局」那个出口定的一致：看不到就当**不存在**，
    不确认"这个 id 存在但你没权限"。

    **变异检验**：把 `_may_read_module` 的调用去掉，这条当场红。
    """
    alice, _bob = world
    async with _session_factory() as db:
        assert await get_module_detail(db, ids["阿贵导入的"], alice) is None


async def test_a_guest_in_the_room_can_still_read_the_intro(world, ids) -> None:
    """🔴 **只按主人过滤会把前情页对非导入者整个关掉。**

    三个调用点全在房间内，而**同房间的其他玩家并不拥有这个模组**——房主导入、
    朋友扫码进来，朋友的 `player_intro` 当场变空白。这条守的正是那半个口子。

    **变异检验**：把 `_may_read_module` 里那段 `Player join Room` 删掉，
    这条当场红。
    """
    alice, bob = world
    async with _session_factory() as db:
        room = Room(
            room_code="OWN001",
            room_name="阿贵开的局",
            max_players=4,
            phase="InGame",
            scenario_id=ids["阿贵导入的"],
        )
        db.add(room)
        await db.flush()
        db.add(Player(room_id=room.id, user_id=alice, nickname="阿福"))
        await db.commit()

    async with _session_factory() as db:
        assert await get_module_detail(db, ids["阿贵导入的"], alice) is not None


async def test_leaving_the_room_is_not_required_to_be_checked_here(world, ids) -> None:
    """在**别的**房间里不算——判据是"这个房间用的就是这份模组"，
    不是"我在某个房间里"。

    **变异检验**：把 `Room.scenario_id == scenario.id` 那个条件删掉，这条当场红。
    """
    alice, _bob = world
    async with _session_factory() as db:
        room = Room(
            room_code="OWN002",
            room_name="用内置模组的局",
            max_players=4,
            phase="InGame",
            scenario_id=ids["内置模组"],
        )
        db.add(room)
        await db.flush()
        db.add(Player(room_id=room.id, user_id=alice, nickname="阿福"))
        await db.commit()

    async with _session_factory() as db:
        assert await get_module_detail(db, ids["阿贵导入的"], alice) is None


def test_the_detail_endpoint_requires_a_logged_in_user() -> None:
    """🔴 端点这一层也要钉住：service 判得再对，控制器忘了要 `get_current_user`
    就等于没做（"整条链都在，就是没人能用到"的镜面——**这次是链上少了一环**）。
    """
    import inspect

    from app.controller.dependencies import get_current_user
    from app.controller.v1.modules import get_module_detail as endpoint

    deps = [
        p.default.dependency
        for p in inspect.signature(endpoint).parameters.values()
        if hasattr(p.default, "dependency")
    ]
    assert get_current_user in deps


# ── 删除（2026-08-19）────────────────────────────────────


async def test_i_can_delete_my_own_import(world, ids) -> None:
    """主人删得掉自己的。"""
    alice_id, _ = world
    async with _session_factory() as db:
        await delete_module(db, ids["阿福导入的"], alice_id)
    async with _session_factory() as db:
        assert await db.get(Scenario, ids["阿福导入的"]) is None


async def test_i_cannot_delete_someone_elses(world, ids) -> None:
    """🔴 别人的删不掉，而且报的是"不存在"不是"没权限"。

    同 `get_module_detail` 的口径：不确认"这个 id 存在但你没权限"。
    """
    alice_id, _ = world
    async with _session_factory() as db:
        with pytest.raises(AppException) as caught:
            await delete_module(db, ids["阿贵导入的"], alice_id)
        assert caught.value.code == ErrorCode.NOT_FOUND
    async with _session_factory() as db:
        assert await db.get(Scenario, ids["阿贵导入的"]) is not None


async def test_a_builtin_module_cannot_be_deleted_by_anyone(world, ids) -> None:
    """🔴 内置模组是随发版进来的目录，任何账号都不该能删掉它。

    它靠 `owner_user_id is None ≠ user_id` 顺带挡住——**不是**另写一条 if。
    **变异检验**：把判断改成 `scenario.owner_user_id not in (None, user_id)`
    （即"无主的谁都能删"），这条当场红。
    """
    alice_id, _ = world
    async with _session_factory() as db:
        with pytest.raises(AppException):
            await delete_module(db, ids["内置模组"], alice_id)
    async with _session_factory() as db:
        assert await db.get(Scenario, ids["内置模组"]) is not None


async def test_a_module_in_use_by_a_room_refuses_to_be_deleted(world, ids) -> None:
    """🔴 有房间在用就拒绝，**并且报出有几个**。

    判据「悬空的指针比没有指针更坏」：`rooms.scenario_id` 没有 ondelete，
    删了那些房间的世界就凭空消失，而复盘/回放还指着它。

    「加一道门必须同时配一条走得通的修法」——所以消息里要说清楚出路（解散），
    数量也要给，否则用户不知道要去解散几个。
    """
    alice_id, _ = world
    module_id = ids["阿福导入的"]
    async with _session_factory() as db:
        for i in range(2):
            db.add(
                Room(
                    room_code=f"USED{i}",
                    room_name=f"房{i}",
                    max_players=4,
                    scenario_id=module_id,
                )
            )
        await db.commit()

    async with _session_factory() as db:
        with pytest.raises(AppException) as caught:
            await delete_module(db, module_id, alice_id)
        assert caught.value.code == ErrorCode.CONFLICT
        assert "2 个房间" in caught.value.message, "要报出数量，否则不知道去解散几个"
        assert "解散" in caught.value.message, "要给出路，不能只说不行"

    async with _session_factory() as db:
        assert await db.get(Scenario, module_id) is not None


async def test_deleting_clears_the_import_jobs_pointer(world, ids) -> None:
    """🔴 **删引用方之前先清指针**（判据：悬空的指针比没有指针更坏）。

    导入任务是历史记录，不删（`retried_from_job_id` 那条链要留着）；但它的
    `result_scenario_id` 必须摘掉，否则导入记录页会拿一个死 id 去开局——那正是
    常用卡那次踩过的坑（下游据此"确信地"显示，然后点了就炸）。
    """
    alice_id, _ = world
    module_id = ids["阿福导入的"]
    async with _session_factory() as db:
        db.add(
            ModuleImportJob(
                owner_user_id=alice_id,
                status="succeeded",
                stage="registering",
                result_scenario_id=module_id,
            )
        )
        await db.commit()

    async with _session_factory() as db:
        await delete_module(db, module_id, alice_id)

    async with _session_factory() as db:
        job = (await db.scalars(select(ModuleImportJob))).one()
        assert job.result_scenario_id is None, "死指针没摘掉"
        assert job.status == "succeeded", "历史记录不该被改写"


async def test_every_table_hanging_off_a_scenario_gets_cleaned(world, ids) -> None:
    """🔴 **扫外键，不逐个列出**——八张表一张都不能漏，而且以后新加的也要被清到。

    这条测试自己也不逐个列出：它从元数据里找出所有指向 `scenarios.id` 的外键，
    给每张表塞一行，删完之后断言全空。**新加一张挂 scenario_id 的表而忘了处理，
    它会自动红**（「逐个列出的地方，加一项就漏一项」）。

    `rooms` 排除在外：走到这里说明没有房间在用它，而且房间本身不该被删模组带走。
    """
    alice_id, _ = world
    module_id = ids["阿福导入的"]

    hanging = [
        (table, fk.parent.name)
        for table in Base.metadata.sorted_tables
        if table.name not in {"scenarios", "rooms", "module_import_jobs"}
        for fk in table.foreign_keys
        if fk.column.table.name == "scenarios"
    ]
    assert hanging, "一张挂着 scenario 的表都没找到 ⇒ 这条测试测了个寂寞"

    async with _session_factory() as db:
        for table, column in hanging:
            values = {column: module_id}
            for col in table.columns:
                if col.name in values or col.nullable or col.default is not None:
                    continue
                if col.primary_key:
                    values[col.name] = str(uuid.uuid4())
                elif str(col.type).upper().startswith(("VARCHAR", "TEXT", "STRING")):
                    values[col.name] = "x"
                elif "JSON" in str(col.type).upper():
                    values[col.name] = {}
                else:
                    values[col.name] = 0
            await db.execute(table.insert().values(**values))
        await db.commit()

    async with _session_factory() as db:
        await delete_module(db, module_id, alice_id)

    async with _session_factory() as db:
        for table, column in hanging:
            left = await db.scalar(
                select(func.count()).select_from(table).where(table.c[column] == module_id)
            )
            assert left == 0, f"{table.name} 没被清干净，留下 {left} 行孤儿"


# ── 同名模组：排序与「第 N 次转换」（2026-08-25） ──────────────


async def _seed_two_conversions_of_one_file(user_id: str) -> tuple[str, str, str]:
    """同一份源文件（同 sha256）转过两次 + 另一份不同文件恰好同名。

    造出**两种同名**：一种是同一份模组的两个版本（代码分得清），一种是真的
    两份不同模组（代码分不清，只能靠日期）。
    """
    from datetime import UTC, datetime, timedelta

    async with _session_factory() as db:
        system_id = (await db.scalars(select(GameSystem.id))).first()
        base = datetime(2026, 8, 10, tzinfo=UTC).replace(tzinfo=None)
        old_id, new_id, other_id = (str(uuid.uuid4()) for _ in range(3))
        db.add_all(
            [
                Scenario(
                    id=old_id,
                    game_system_id=system_id,
                    title="林中屋",
                    owner_user_id=user_id,
                    created_at=base,
                    updated_at=base,
                ),
                Scenario(
                    id=new_id,
                    game_system_id=system_id,
                    title="林中屋",
                    owner_user_id=user_id,
                    created_at=base + timedelta(days=15),
                    updated_at=base + timedelta(days=15),
                ),
                # 🔴 **不同文件、恰好同名**：它不该被算成"第 3 次转换"
                Scenario(
                    id=other_id,
                    game_system_id=system_id,
                    title="林中屋",
                    owner_user_id=user_id,
                    created_at=base + timedelta(days=20),
                    updated_at=base + timedelta(days=20),
                ),
            ]
        )
        for scenario_id, digest, offset, status in (
            (old_id, "aaa", 0, "succeeded"),
            # 🔴 中间夹一次**失败**的：它没有产物，不该占掉一个次序号
            # （否则新的那份会被标成"第 3 次"，而列表里只有两份）。
            (None, "aaa", 7, "failed"),
            (new_id, "aaa", 15, "succeeded"),
            (other_id, "bbb", 20, "succeeded"),
        ):
            db.add(
                ModuleImportJob(
                    id=str(uuid.uuid4()),
                    owner_user_id=user_id,
                    status=status,
                    stage="registering",
                    source_sha256=digest,
                    result_scenario_id=scenario_id,
                    created_at=base + timedelta(days=offset),
                    updated_at=base + timedelta(days=offset),
                )
            )
        await db.commit()
        return old_id, new_id, other_id


async def test_the_newest_import_comes_first(world) -> None:
    """🔴 列表必须有确定的顺序：两份同名时，刚导进来的那份在最上面。

    此前 `list_modules` 一个 `order_by` 都没有——顺序由数据库随便挑，而
    "随便挑"意味着两份同名的模组每次刷新都可能换位置，选错就是一局零线索。
    变异检验：删掉 `order_by`，SQLite 会按插入序返回，这条红。
    """
    alice_id, _ = world
    old_id, new_id, other_id = await _seed_two_conversions_of_one_file(alice_id)

    async with _session_factory() as db:
        modules = await list_modules(db, user_id=alice_id)

    # 内置在最前（它们是首选）
    assert modules[0].is_imported is False
    imported = [m.id for m in modules if m.is_imported]
    # 导入的按时间倒序：最新那份紧跟在内置之后
    assert imported.index(other_id) < imported.index(new_id) < imported.index(old_id)


async def test_two_conversions_of_the_same_file_are_numbered(world) -> None:
    """🔴 同一份源文件的两次转换标成「第 N 次 / 共 M 次」，**不同文件不算进来**。

    变异检验：把分组键从 `source_sha256` 换成 `title`，那份"不同文件恰好同名"
    的会被算成第 3 次，这条当场红。
    """
    alice_id, _ = world
    old_id, new_id, other_id = await _seed_two_conversions_of_one_file(alice_id)

    async with _session_factory() as db:
        by_id = {m.id: m for m in await list_modules(db, user_id=alice_id)}

    assert (by_id[old_id].conversion_index, by_id[old_id].conversion_total) == (1, 2)
    assert (by_id[new_id].conversion_index, by_id[new_id].conversion_total) == (2, 2)
    # 🔴 另一份文件恰好同名 ⇒ 它自己是"第 1 次 / 共 1 次"，前端据此不显示标记
    assert (by_id[other_id].conversion_index, by_id[other_id].conversion_total) == (1, 1)


async def test_builtin_modules_have_no_conversion_numbers(world) -> None:
    """内置模组不是转换出来的 ⇒ 两个字段都是 null（而不是 1/1）。

    🔴 "不适用"与"转过一次"是两件事，压成同一个值前端就没法区分该不该显示标记。
    """
    alice_id, _ = world
    async with _session_factory() as db:
        builtin = next(m for m in await list_modules(db, user_id=alice_id) if not m.is_imported)
    assert builtin.conversion_index is None
    assert builtin.conversion_total is None
