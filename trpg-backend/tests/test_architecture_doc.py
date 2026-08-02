"""`keeper/ARCHITECTURE.md` 不许跟代码说的不一样。

## 为什么值得一条测试

这份文档是**新人入口**：它列的那张钩子表就是"加一个能力要实现什么"的清单。
表漏一行，下一个人就漏一个钩子——而漏钩子的后果全是静默的（那片能力在日志里
隐身、它的记账能被 `state_updates` 覆盖、结算找不到认领者）。

同项目反复吃过的亏：**同一份知识在两个地方各写一遍，迟早不一致，而不一致的
那一刻不会有任何东西变红。** 写这条测试时就抓到一次——文档说"八个钩子"，
`KeeperCapability` 实际有 9 个字段（`field_capabilities` 被我当成了 `schema`
的附属）。
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

from app.core.keeper.contract.registry import KeeperCapability

_DOC = pathlib.Path(__file__).resolve().parents[1] / "app" / "core" / "keeper" / "ARCHITECTURE.md"


def test_the_hook_table_lists_every_capability_field() -> None:
    text = _DOC.read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| `(\w+)` \|", text, flags=re.M))
    actual = {f.name for f in dataclasses.fields(KeeperCapability)} - {"name"}
    missing = actual - documented
    stale = documented - actual
    assert not missing, f"ARCHITECTURE.md 的钩子表漏了：{sorted(missing)}"
    assert not stale, f"ARCHITECTURE.md 的钩子表里有已经不存在的：{sorted(stale)}"


def test_every_directory_the_doc_names_actually_exists() -> None:
    """目录树写错会让新人在不存在的路径里找东西。"""
    text = _DOC.read_text(encoding="utf-8")
    keeper = _DOC.parent
    named = set(
        re.findall(
            r"`(?:capabilities|runtime|contract|primitives|memory|access|narration)/[\w./]*`", text
        )
    )
    for token in named:
        rel = token.strip("`")
        if rel.endswith("/"):
            rel = rel[:-1]
        # 形如 `runtime/{a,b}` 的花括号简写跳过
        if "{" in rel or "*" in rel:
            continue
        assert (keeper / rel).exists(), f"ARCHITECTURE.md 写了 {rel}，但它不存在"
