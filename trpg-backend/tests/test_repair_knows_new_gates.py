"""自修器必须认识每一类硬失败（`exec/29` 第 3 步落地记录）。

## 为什么有这个文件

`exec/29` 加了两道忠实度门（`trace` / `numeric`），但**没更新自修器**。实测林中屋
端到端跑完出 3 条 numeric 错误，自修跑了 1 轮**一处都没动**——因为它的 system
prompt 逐条列了该修什么（技能名 / 引用 / visibility_pairs / 泄密 / structure），
唯独没有这两类。

这正是项目那条判据：**「骨架里每一处『逐个列出各能力字段』的地方，要么是个还没
被发现的钩子，要么是个还没被识别的权限门」**——`repair_module` 的 prompt 就是
这种枚举，而枚举一定会漏。

## 🔴 还有一件比 prompt 更根本的

自修器原本**拿不到原文**（只有错误清单 + 模组 JSON）。而 `trace`/`numeric` 说的
就是「跟原文对不上」——不给它原文，它只能靠猜，**而猜正是这两道门要禁的**。
所以修法是两半：prompt 补类别 + **把出问题实体的源行一起给它**。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.module_probe import assemble  # noqa: E402
from scripts.module_probe.validate_module import ValidationReport  # noqa: E402


def _report() -> ValidationReport:
    return ValidationReport(
        ok=False,
        schema_ok=True,
        trace_errors=["node 'kitchen' 与源行的最长逐字重合仅 1 字（下限 3）——疑似脱离原文编造"],
        numeric_errors=["node 'mi-go' 的数值 '701d6+1' 在原文里找不到——疑似凭空生成"],
    )


def test_every_hard_failure_category_appears_in_the_repair_prompt() -> None:
    """🔴 每一类硬失败都要在自修 prompt 里有对应说明。

    加一道门却不更新这里，那道门产生的错误就永远修不掉 —— 而且不会有任何东西
    变红，只是拒绝率悄悄变高。
    """
    prompt = assemble.repair_module.__doc__ or ""
    import inspect

    body = inspect.getsource(assemble.repair_module)

    for category in ("numeric", "trace", "技能名", "leads_to", "visibility_pairs", "不泄密"):
        assert category in body, f"自修 prompt 没提 {category}，那一类错误它不会修"
    assert prompt  # 函数本身要有文档


def test_failing_entity_ids_are_extracted_for_the_excerpt() -> None:
    ids = assemble._fidelity_entity_ids(_report())

    assert ids == ["kitchen", "mi-go"]


def test_only_the_failing_entities_source_is_included() -> None:
    """🔴 只给出问题的那几个实体，不给全文。

    全文会把 prompt 撑爆也更贵，而自修要的只是「这一段原文到底怎么写的」。
    """
    anchors = {"kitchen": "厨房水槽", "mi-go": "爪击70 1D6+1", "cellar": "地窖木梯"}

    excerpt = assemble._source_excerpt(anchors, ["kitchen", "mi-go"])

    assert "厨房水槽" in excerpt and "爪击70 1D6+1" in excerpt
    assert "地窖木梯" not in excerpt, "没出问题的实体不该进 prompt"


def test_excerpt_is_empty_when_nothing_failed() -> None:
    assert assemble._source_excerpt({"a": "x"}, []) == ""
