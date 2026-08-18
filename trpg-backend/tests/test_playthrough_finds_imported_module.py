"""试跑驱动器必须够得到**导入的**模组。

## 这是个不对称，不是权限设计

导入的模组带 `owner_user_id`，`GET /api/v1/modules` 因此按归属过滤（否则每个人
都看得见别人导入的模组）。而试跑驱动器**注册的是一个全新用户**——它在目录里
一条导入模组都看不到。

不对称在于：驱动器不是"没权限玩"，是"发现不到"。它拿着一个合法的 scenario id
却被目录接口挡在门外，症状是「模组 id 不在目录里」——听起来像 id 写错了，
其实是归属过滤。

🔴 **2026-08-18 修正**：这段原来写着「选模组那一步（`select_module`）不查归属
（同房间其他玩家本来就不需要拥有它）」，并把它当成有意的设计。**理由跟动作对
不上**——`select_module` 只有房主调得动，"同房间其他玩家"根本走不到这条路；
真正必须对非拥有者开放的是**在房间里玩**，而那条读的是 `rooms.scenario_id`，
一点不受影响。于是「列表里看不见、拿着 id 照样开得起来」成了归属规则的一个
缺口，已补上（`select_module` 现在也查归属）。

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
from app.models.room import Player, Room
from app.models.user import User
from app.service import room as room_service

_FIXTURE = Path(__file__).parent / "fixtures" / "keeper_module.json"

#: `reconnect_token` 是 UUID 列，随手写个字符串会在插入时炸。
_STRANGER_TOKEN = str(uuid.uuid4())
_OWNER_TOKEN = str(uuid.uuid4())


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


async def test_choosing_it_needs_ownership_too(owned_import) -> None:
    """🔴 归属规则要落在**每一个出口**上：列表里藏起来了，开局那一步也得拦。

    此前只校验"模组存在"，所以拿着一个从别处知道的 scenario id 照样开得起来
    ——列表过滤等于形同虚设。同族判据：**一份数据有几个出口，规则就要落几处**。

    报的是「模组不存在」而不是「无权使用」：后者等于替对方确认这个 id 有效。
    """
    from app.dto.room import SelectModuleBody

    factory, _modules_dir, scenario_id, owner_id = owned_import

    async with factory() as db:
        stranger = User(account="stranger", password_hash="x", nickname="路人")
        db.add(stranger)
        await db.flush()
        room = Room(
            room_code="OWN001",
            room_name="路人的房间",
            max_players=4,
            phase="Lobby",
        )
        db.add(room)
        await db.flush()
        host = Player(
            room_id=room.id,
            nickname="路人",
            is_host=True,
            user_id=stranger.id,
            reconnect_token=_STRANGER_TOKEN,
        )
        db.add(host)
        await db.flush()
        room.host_player_id = host.id
        await db.commit()
        room_id = room.id

    body = SelectModuleBody(module_id=scenario_id, attribute_gen_method="point_buy")
    async with factory() as db:
        with pytest.raises(room_service.ModuleNotFoundError):
            await room_service.select_module(db, room_id, body, _STRANGER_TOKEN)


async def test_the_owner_can_still_choose_it(owned_import) -> None:
    """🔴 加了门就得有一条走得通的路——主人自己照常开得起来。"""
    from app.dto.room import SelectModuleBody

    factory, _modules_dir, scenario_id, owner_id = owned_import

    async with factory() as db:
        room = Room(room_code="OWN002", room_name="主人的房间", max_players=4, phase="Lobby")
        db.add(room)
        await db.flush()
        host = Player(
            room_id=room.id,
            nickname="主人",
            is_host=True,
            user_id=owner_id,
            reconnect_token=_OWNER_TOKEN,
        )
        db.add(host)
        await db.flush()
        room.host_player_id = host.id
        await db.commit()
        room_id = room.id

    body = SelectModuleBody(module_id=scenario_id, attribute_gen_method="point_buy")
    async with factory() as db:
        await room_service.select_module(db, room_id, body, _OWNER_TOKEN)
        refreshed = await db.get(Room, room_id)
        assert refreshed.scenario_id == scenario_id
