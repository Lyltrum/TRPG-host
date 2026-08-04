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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import Base
from app.models.content import Game, GameSystem, Scenario
from app.models.user import User
from app.service.room import list_modules

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
