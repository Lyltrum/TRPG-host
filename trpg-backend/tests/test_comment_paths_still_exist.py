"""注释和文档字符串里提到的 `app/...` 路径必须还存在。

## 为什么需要它

`exec/27` 阶段 1 和阶段 5 两次搬目录之后，代码里留下 **17 处**指向旧路径的
注释——全套测试、ruff、ty、e2e 全绿，因为没有任何机器检查会去读注释。

最刺眼的两条：

- `tests/test_capability_registry.py` 那条**报错文案**指路 `keeper/decision.py`，
  而它出现的时机恰恰是新人漏了 schema 注册那一步的时候——最需要指路的一刻
  指错了路。
- `primitives/skills.py` 声明的不变量点名 `tools._resolve_skill_target` 作为
  执行层锚点，而那个函数在切能力时就没了。**不变量还在，锚点没了。**

> 🔴 **「文档也算另一个地方」，而注释也是文档**——只是它不在 `.md` 里，
> 所以躲过了只扫 `CLAUDE.md` / `AGENTS.md` 的那轮校验。

## 边界

只认 `app/` 开头的路径（后端包根，全仓库唯一）。像 `tests/conftest.py` 这种
相对某个目录才成立的写法不在范围内——把它们纳进来只会造出一堆误报，而实际
坏掉的那 17 条全部是 `app/` 开头的。

历史叙述（"此前本模块 import 过 X"）也要跟着改：读注释的人分不清哪句是史料，
一个指不到东西的路径无论出于什么理由都会浪费他一次查找。
"""

from __future__ import annotations

import pathlib
import re

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent

#: 扫哪些地方。前端也扫：`games.ts` 里那条「scenario_id 必须与后端 catalog.py
#: 一致」是**跨仓库契约**，指错路等于这条约束没人守得住。
_SCAN_ROOTS = (
    _BACKEND / "app",
    _BACKEND / "tests",
    _BACKEND / "scripts",
    _BACKEND / "alembic",
    _REPO / "trpg-frontend" / "src",
    _REPO / "trpg-sdk" / "src",
)

_SUFFIXES = {".py", ".ts", ".tsx", ".md"}
_SKIP_DIRS = {"__pycache__", "node_modules", "dist", "generated"}

#: 形如「app 斜杠 某目录 斜杠 某文件.py」——允许中文文件名。
#: （这行故意不写成真的路径样子：它会被自己扫到。`模组资料/` 那类不以 app 开头，不在范围内。）
_PATH = re.compile(r"app/[\w一-鿿./-]*\.(?:py|ts|tsx|md|json)")


def _files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for root in _SCAN_ROOTS:
        if not root.is_dir():
            continue
        out += [
            p
            for p in root.rglob("*")
            if p.suffix in _SUFFIXES and not _SKIP_DIRS & set(p.parts) and p.is_file()
        ]
    return sorted(out)


def test_there_are_files_to_scan() -> None:
    """🔴 防空转：扫描根写错了它会全绿而什么都没查。"""
    assert len(_files()) >= 100


@pytest.mark.parametrize("path", _files(), ids=lambda p: str(p.relative_to(_REPO)))
def test_every_app_path_mentioned_in_this_file_exists(path: pathlib.Path) -> None:
    stale: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for mentioned in _PATH.findall(line):
            if not (_BACKEND / mentioned).exists():
                stale.append(f"  第 {lineno} 行：{mentioned}")
    assert not stale, (
        f"{path.relative_to(_REPO)} 提到了已经不存在的路径：\n"
        + "\n".join(stale)
        + "\n（搬文件时注释要跟着走——指错路的注释比没有注释更费时间。）"
    )
