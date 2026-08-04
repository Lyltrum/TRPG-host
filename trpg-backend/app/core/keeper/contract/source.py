"""模组来源接缝：一个接口，两个来源（`exec/29` 第 1 步）。

在这之前，"哪个 scenario 玩哪份剧本"只有一条路——`catalog.py` 里那个**编译期
常量元组**。导入的模组没有任何人为它写过代码行，于是永远解析不出来
（`tests/test_module_runtime_registry.py` 就是先把这件事证伪的那条用例）。

    resolve_module(db, modules_dir, scenario_id) → ResolvedModule | None
        ├─ 内置：catalog 常量 → `模组资料/*.structured.json`   （随发版进来）
        └─ 导入：`imported_modules` 表 → structured JSON        （运行时数据）

🔴 **统一的是接口，不是存储。** 内置模组**不落库**——判据见 `ImportedModule` 的
类文档（「随发版进来的东西不进数据库」）。上游四个调用点只认这个接缝，不知道
内容从哪来；以后再加来源（比如远端模组市场）也只动这一个文件。

🔴 **返回的是已加载的 `ScenarioModule`，不是路径。** 数据库来源根本没有路径，
让接缝返回 `Path` 会逼着调用方处理"有时是路径有时不是"。`load_module` 因此收敛
成只在本文件内被调用一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.keeper.contract.catalog import resolve_structured_path
from app.core.keeper.contract.module_loader import ScenarioModule, load_module


@dataclass(frozen=True, slots=True)
class ResolvedModule:
    """一份解析好的剧本，外加一个可用于缓存的稳定键。

    `cache_key` 存在的理由：`RoomAwareKeeperNarrator` 按剧本缓存 `KeeperAgent`，
    原先用的是文件路径。数据库来源没有路径，所以键由本模块给出——内置用路径、
    导入用 scenario id，两边都稳定且不会互撞。
    """

    cache_key: str
    module: ScenarioModule


async def resolve_module(
    db: AsyncSession,
    modules_dir: Path,
    scenario_id: str | None,
) -> ResolvedModule | None:
    """按 scenario 解析剧本；解析不出来返回 `None`（调用方决定怎么降级）。

    先查内置：内置模组是随发版进来的，它的语义比数据库里任何一行都权威。
    """
    if not scenario_id:
        return None

    path = resolve_structured_path(modules_dir, scenario_id)
    if path is not None:
        return ResolvedModule(cache_key=str(path), module=load_module(path))

    return await _resolve_imported(db, scenario_id)


async def _resolve_imported(db: AsyncSession, scenario_id: str) -> ResolvedModule | None:
    # 局部 import：`keeper/` 是领域层，让它在模块顶层依赖 `app.models` 会把
    # ORM 拖进每一条 keeper 的 import 链。这里是唯一需要它的地方。
    from app.models.content import ImportedModule

    row = await db.get(ImportedModule, scenario_id)
    if row is None:
        return None
    return ResolvedModule(
        cache_key=f"imported:{scenario_id}",
        module=ScenarioModule.model_validate(row.structured),
    )
