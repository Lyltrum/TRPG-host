"""迁移文件里 import 的应用模块必须还存在。

## 为什么需要它

`exec/27` 阶段 5 把 `coc7_content.py` 挪进 `coc7/`，一条**已合并的历史迁移**
里那句 `from app.core.coc7_content import ...` 当场失效——新库执行到那一步会
直接炸。

而**全套测试照样绿**：测试建表走的是 `Base.metadata.create_all`，根本不经过
迁移。真正抓到它的是 `ty`（它的 `src.include` 恰好包含 `alembic`），属于运气。

> 🔴 **迁移是历史记录，但它引用的应用代码是活的。** 模块一搬家，历史迁移就
> 可能坏掉，而坏掉的地方离你改的东西很远。

这条测试只做静态检查（import 得到就算过），不真跑迁移——真跑要起库、慢，
且 CI 里另有 `alembic upgrade head` 的位置。静态检查足以挡住"模块搬走了"
这一类，而那正是实际发生过的那一类。
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

_ALEMBIC = pathlib.Path(__file__).resolve().parents[1] / "alembic"


def _app_imports(path: pathlib.Path) -> set[str]:
    """这个迁移文件 import 了哪些 `app.*` 模块（含函数体内的延迟导入）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return {m for m in found if m.startswith("app.")}


def _migration_files() -> list[pathlib.Path]:
    return sorted(_ALEMBIC.rglob("*.py"))


def test_there_are_migrations_to_check() -> None:
    """🔴 防止这份用例变成空转：目录空了它会全绿而什么都没验。"""
    assert len(_migration_files()) >= 5


@pytest.mark.parametrize("path", _migration_files(), ids=lambda p: p.name)
def test_every_module_a_migration_imports_still_exists(path: pathlib.Path) -> None:
    for module in sorted(_app_imports(path)):
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as exc:  # pragma: no cover - 只在真坏了时走到
            # 用 assert 而不是 pytest.fail：后者被 @_with_exception 包过，
            # ty 推不出它的真实签名（同 test_narrator.py 里那处）。
            raise AssertionError(
                f"{path.name} 里 import 的 {module} 已经不存在了（{exc}）。"
                "模块搬家时要一并改历史迁移——它们在新库上是会被执行的。"
            ) from exc
