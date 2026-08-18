"""导入的模组也得有年代：写背景与装备审核共用的那个取值口。

🔴 **这条断言写出来的时候是红的**（2026-08-18）：`character_background` 那份
取值还停在 `resolve_structured_path`——只认 `catalog.py` 里那个编译期常量元组，
**导入的模组没有文件路径** ⇒ `era` 恒为 `None` ⇒ 写背景的模型永远拿不到年代。
而装备审核那份两天前就已经走了 `resolve_module` 接缝。同一个问题两份取值、
其中一份是旧写法，正是「改了口径只改一半」。

修法是两处共用 `character_background.module_era`。

🔴 **2026-08-18 晚又收窄了一次**：它原来同时返回 `meta.tone`，而那个字段是
KP 侧的（跟绝密真相并排渲进守秘人 prompt），六份模组里有一份往里写了谜底。
玩家侧的两个消费方现在只拿得到 `era`。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.db import Base
from app.models.content import Game, GameSystem, ImportedModule, Scenario
from app.service.character_background import module_era

_FIXTURE = Path(__file__).parent / "fixtures" / "keeper_module.json"


@pytest.fixture
async def imported_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """一条 `imported_modules` 行 + 一个**空**的模组目录。

    空目录是关键：它把"按文件路径找"这条路彻底堵死，剩下能通的只有接缝。
    不钉死目录的话会去扫开发机真实的 `模组资料/`（同 `test_keeper_agent` 那条
    环境泄漏注释）。
    """
    import app.service.character_background as mod

    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    monkeypatch.setattr(mod, "get_settings", lambda: Settings(keeper_modules_dir=str(modules_dir)))

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/era.db", poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    scenario_id = str(uuid.uuid4())
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
        await db.commit()

    yield factory, scenario_id, structured["meta"]
    await engine.dispose()


async def test_imported_module_still_has_an_era(imported_only) -> None:
    factory, scenario_id, meta = imported_only

    async with factory() as db:
        era = await module_era(db, scenario_id)

    assert era == meta["era"], "导入的模组没有文件路径，取值必须走接缝而不是按路径找"


async def test_unknown_scenario_degrades_to_none(imported_only) -> None:
    """取不到就 `None`——建卡不该因为模组解析不出来而失败。"""
    factory, _scenario_id, _meta = imported_only

    async with factory() as db:
        assert await module_era(db, str(uuid.uuid4())) is None
        assert await module_era(db, None) is None
