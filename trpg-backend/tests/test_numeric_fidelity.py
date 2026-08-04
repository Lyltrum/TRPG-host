"""数值忠实度：产物里的骰型/百分比必须在原文里出现过（`exec/29 §4.6 ⑥`）。

## 它解决什么

词面兜底看的是「有没有关系」，拦不住这个：实体跟源行大量重合、结构完全合法，
**但里面的数字被改了**。而数值是模组里**唯一具有清晰对错的东西**——散文改写
没有对错（「积满灰尘」写成「布满尘埃」都行），`1d6` 写成 `1d4` 就是错。

只有这道门能抓到骰型被改：`validate` 说结构合法，词面兜底说重合充足，
AI 玩家试跑少扣两点 SAN 照样通关。

## 🔴 它当前几乎零命中，这是如实的

五份模组实测：归一之后 **95.7–100% 的数值能在原文里定位**。剩下 3 个是同一种
模式——模型把散文规则（「失败则会失去 1D10 的理智」）转成 COC7 记号时补了原文
没有的值。**所以这道门守的是「以后会不会错」，不是「现在错了」。**

## 三个作用域决定（都是实测标出来的）

1. **判据是全文，不是源行。** 源行级假阳性太高（复足 21%）——组装会跨片段合并，
   而锚点是片段粒度。全文级只剩 3 个。代价是抓不到「数值从别的场景搬过来」，
   那属于错位，本来就归 AI 玩家试跑。
2. **只查骰型与百分比，不查孤立整数。** id 序号、年龄、数量词噪声太大。
3. **必须归一大小写与全角**（NFKC + lower）。不归一会报 8 个假阳性，全是原文写
   `1D6`、产物写 `1d6`。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.module_probe.validate_module import check_numeric_fidelity  # noqa: E402

SOURCE = [
    "米-戈现身时，调查员需通过理智检定，失败则会失去 1D10 的理智。",
    "霰弹枪造成 4D6 伤害；徒手拳击 1D3。",
    "锁着的箱子需要 75% 的锁匠检定才能打开。",
]


def _module(kp_text: str) -> dict:
    return {"meta": {"id": "m", "title": "t"}, "nodes": [{"id": "n1", "kp_text": kp_text}]}


def test_dice_present_in_source_passes() -> None:
    assert check_numeric_fidelity(_module("失败失去 1d10 理智"), SOURCE) == []


def test_dice_absent_from_source_is_hard_failure() -> None:
    """骰型被改 → 唯一能抓到它的就是这道门。"""
    errors = check_numeric_fidelity(_module("失败失去 1d4 理智"), SOURCE)

    assert any("1d4" in e for e in errors)


def test_case_and_width_are_normalized() -> None:
    """🔴 原文写 `1D10`、产物写 `1d10`，不归一就是 8 个假阳性（实测踩过）。"""
    assert check_numeric_fidelity(_module("造成 4d6 伤害"), SOURCE) == []
    assert check_numeric_fidelity(_module("造成 ４Ｄ６ 伤害"), SOURCE) == []


def test_san_pair_notation_only_checks_its_dice_part() -> None:
    """`1/1d3` 拆开只验骰型分量。

    原文里 SAN 损失常常是散文（「失去 1D3 理智」），从来没有 `x/y` 成对记号——
    整体比对等于对每个写散文的模组都报错。允许格式转换，但骰型写错必被抓到。
    """
    assert check_numeric_fidelity(_module("理智损失 1/1d3"), SOURCE) == []

    errors = check_numeric_fidelity(_module("理智损失 1/1d8"), SOURCE)
    assert any("1d8" in e for e in errors)


def test_percentage_is_checked() -> None:
    assert check_numeric_fidelity(_module("需要 75% 检定"), SOURCE) == []
    assert any("40%" in e for e in check_numeric_fidelity(_module("需要 40% 检定"), SOURCE))


def test_bare_integers_are_not_checked() -> None:
    """孤立整数不查——id 序号 / 年龄 / 数量词噪声太大，查了全是假阳性。"""
    assert check_numeric_fidelity(_module("他今年 42 岁，房里有 7 个箱子"), SOURCE) == []


def test_no_source_means_check_is_skipped_not_passed() -> None:
    """没有原文就无从比对——整条跳过，别让"没报错"被当成"过了"。"""
    assert check_numeric_fidelity(_module("造成 9d9 伤害"), []) == []
