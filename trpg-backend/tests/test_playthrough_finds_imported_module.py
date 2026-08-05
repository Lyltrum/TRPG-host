"""试跑驱动器必须够得到**导入的**模组。

## 这是个不对称，不是权限设计

导入的模组带 `owner_user_id`，`GET /api/v1/modules` 因此按归属过滤（否则每个人
都看得见别人导入的模组）。而试跑驱动器**注册的是一个全新用户**——它在目录里
一条导入模组都看不到。

不对称在于：**选模组那一步（`select_module`）不查归属**（同房间其他玩家本来就
不需要拥有它）。所以驱动器不是"没权限玩"，是"发现不到"。它拿着一个合法的
scenario id 却被目录接口挡在门外，症状是「模组 id 不在目录里」——听起来像 id
写错了，其实是归属过滤。

修法是让驱动器走 `resolve_module` 直接解析剧本，而不是从目录接口找。它是服务端
工具，本来就有库访问；顺带少一次 HTTP 往返。

## 这条用例守什么

**归属过滤和试跑发现路径的这个不对称**。下一个人给目录接口加过滤条件时，
这条会红——而不是等到某次试跑莫名其妙报「模组 id 不在目录里」。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import Base
from app.core.keeper.contract.source import resolve_module
from app.models.content import Game, GameSystem, ImportedModule, Scenario
from app.models.user import User
from app.service import room as room_service

_FIXTURE = Path(__file__).parent / "fixtures" / "keeper_module.json"


@pytest.fixture
async def owned_import(tmp_path: Path):
    """一份**属于某个用户**的导入模组——这正是真机上的形状。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/owned.db", poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    scenario_id = str(uuid.uuid4())

    async with factory() as db:
        owner = User(account="owner", password_hash="x", nickname="主人")
        db.add(owner)
        game = Game(name="跑团")
        db.add(game)
        await db.flush()
        system = GameSystem(game_id=game.id, name="COC7")
        db.add(system)
        await db.flush()
        db.add(
            Scenario(
                id=scenario_id,
                game_system_id=system.id,
                owner_user_id=owner.id,
                title="导入的模组",
                players_min=1,
                players_max=4,
            )
        )
        await db.flush()
        db.add(
            ImportedModule(
                scenario_id=scenario_id,
                structured=json.loads(_FIXTURE.read_text(encoding="utf-8")),
            )
        )
        await db.commit()
        owner_id = owner.id

    yield factory, modules_dir, scenario_id, owner_id
    await engine.dispose()


async def test_catalog_hides_it_from_another_user(owned_import) -> None:
    """先钉住那个不对称的前半段：换个用户，目录里查无此模组。

    这是**对的行为**（别人导入的模组不该出现在我的列表里），这条用例存在是为了
    说明下一条不是多余的。
    """
    factory, _modules_dir, scenario_id, _owner_id = owned_import

    async with factory() as db:
        visible = await room_service.list_modules(db, user_id="another-user-id")

    assert scenario_id not in [m.id for m in visible]


async def test_the_driver_can_still_resolve_it(owned_import) -> None:
    """🔴 后半段：驱动器靠 `resolve_module` 拿到剧本，不经过目录接口。

    没有这条，试跑就只能跑内置模组——而它存在的意义恰恰是验刚导进来的那份。
    """
    factory, modules_dir, scenario_id, _owner_id = owned_import

    async with factory() as db:
        resolved = await resolve_module(db, modules_dir, scenario_id)

    assert resolved is not None, "拿着合法 scenario id 却解析不出剧本，试跑就无从谈起"
    assert resolved.module.nodes
    assert resolved.module.meta.title
