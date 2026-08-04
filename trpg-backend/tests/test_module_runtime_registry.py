"""模组导入：运行时注册的模组必须能被玩起来（`exec/29` 第 0/1 步）。

**这两条断言写出来的时候是红的**——那是第 0 步的全部目的：在不接 LLM、不做
上传、不碰 UI 的前提下，先量出第 1 步到底要动多少东西。同族先例：`exec/28`
开工第一步是先写等价性测试。

断言是：

    「一份合法的 structured 内容 + 数据库里一条 scenario 行」
    ⇒ 绑定它的房间应当能加载出模组。

当时不成立：`resolve_structured_path` 只认 `KEEPER_MODULE_SPECS` 这个**编译期
常量元组**，数据库里的行它一概看不见 → 落到 `KEEPER_MODULE_PATH` 兜底 → 抛
`FileNotFoundError`。第 1 步做的正是"模组从编译期常量变成运行时数据"。

第 1 步已按「导入的 structured 落库」实现，所以本文件提供内容的方式是往
`imported_modules` 插一行；**断言从证伪那一版起一字未改**。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import Base
from app.core.narration.room_aware import RoomAwareKeeperNarrator
from app.models.content import Game, GameSystem, ImportedModule, Scenario
from app.models.room import Room

_FIXTURE = Path(__file__).parent / "fixtures" / "keeper_module.json"


@pytest.fixture
async def imported_scenario(tmp_path: Path):
    """造一个**不在 catalog 里**的模组：新 UUID + 数据库行 + structured 内容。

    模拟的正是导入的产物——没有任何人为它写过代码行。
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/import.db", poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    scenario_id = str(uuid.uuid4())
    # 空目录：导入的模组**不在** `模组资料/` 里，正是要证的那件事。
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    structured = json.loads(_FIXTURE.read_text(encoding="utf-8"))

    async with factory() as db:
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
                title="导入的模组",
                players_min=1,
                players_max=4,
            )
        )
        await db.flush()
        db.add(ImportedModule(scenario_id=scenario_id, structured=structured))
        room = Room(
            room_code="IMPT01",
            room_name="导入房",
            max_players=4,
            phase="InGame",
            scenario_id=scenario_id,
        )
        db.add(room)
        await db.commit()
        ids = (room.id, scenario_id)

    yield factory, modules_dir, ids
    await engine.dispose()


async def test_runtime_registered_scenario_is_resolvable(imported_scenario) -> None:
    """catalog 是编译期常量，看不见数据库里的 scenario 行——接缝要能补上这一段。"""
    factory, modules_dir, (room_id, _scenario_id) = imported_scenario
    narrator = RoomAwareKeeperNarrator(
        api_key="test-key",
        modules_dir=modules_dir,
        fallback_path=None,
        session_factory=factory,
    )

    resolved = await narrator._resolve(room_id)

    assert resolved.module.nodes, "运行时注册的模组必须能解析出可玩的剧本"


async def test_runtime_registered_scenario_loads_a_playable_module(imported_scenario) -> None:
    """解析出来还不够——必须真的能建出 agent，否则只是查到了一行。

    走到 `_agent_for` 为止就停：再往下 `narrate` 要发真实网络请求，而叙事那条
    链早就跑过几十局了，不是这里要证的事。
    """
    factory, modules_dir, (room_id, _scenario_id) = imported_scenario
    narrator = RoomAwareKeeperNarrator(
        api_key="test-key",
        modules_dir=modules_dir,
        fallback_path=None,
        session_factory=factory,
    )

    resolved = await narrator._resolve(room_id)
    agent = narrator._agent_for(resolved)

    assert agent is not None
    # 同一个 scenario 复用同一个 agent（模块级缓存），别每轮重新解析
    assert narrator._agent_for(resolved) is agent


def test_fixture_is_a_valid_structured_module() -> None:
    """脚手架自检：这份原创迷你剧本本身必须是合法 structured。

    否则上面两条红了也说明不了问题——分不清是架构缺口还是 fixture 坏了。
    """
    from app.core.keeper.contract.module_loader import ScenarioModule

    module = ScenarioModule.model_validate(json.loads(_FIXTURE.read_text(encoding="utf-8")))

    assert module.nodes, "迷你剧本至少要有一个节点"
