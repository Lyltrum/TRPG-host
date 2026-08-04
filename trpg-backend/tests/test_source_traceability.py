"""忠实度硬门：组装出来的每个实体都要能回溯到原文（`exec/29 §4`）。

## 为什么是这个形状

`exec/29` 原方案是「让模型在 schema 里必填 `(片段 id, 原文引文)`」。实测之后
改了：**锚点已经存在**——裸抽取的每条片段都带 `line_start`/`line_end`，加上
阶段 1 的归组映射，就能定位到原文行。它跟引文一样是模型自报、一样可以编造，
但**同样机械可验证**，而且省掉一次 schema 改动 + 一次全量重组装。

🔴 **实测发现链是断的**：25–57% 的实体没有锚点，而且断口非常整齐——
**几乎全是 `sub_nodes`**（林中屋 11/11、死者的顿足舞 21/21 都是子节点），
顶层实体几乎 100% 有锚点。子节点是组装阶段现拆的，没人给它们溯源。
所以这里的第一件事是**让子节点继承祖先的锚点**。

## 判据

- **无锚点 = 硬失败**（继承之后还没有的，就是真的凭空长出来的）。信号清晰、二值。
- **有锚点者：与源行的最长逐字重合 ≥ `MIN_TRACE_RUN`**——只是**兜底**，见该常量
  的说明：实测重合值是连续分布、没有自然断点，这道门分不出「改写」和「编造」。

手法与 `leak_guard` 判泄密同源——**逐字重合，不问模型**。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.module_probe.validate_module import (  # noqa: E402
    MIN_TRACE_RUN,
    check_source_traceability,
)

# 原文：行号 1-based
SOURCE = [
    "宅邸的门厅积满灰尘，地板上有拖拽的痕迹一路通向厨房。",  # 1
    "厨房的水槽里泡着一只缺口的搪瓷碗，水面浮着薄薄一层油膜。",  # 2
    "餐桌下压着一张揉皱的收据，日期是三个月前。",  # 3
    "地窖的木梯第四级断了，下面传来持续的低频嗡鸣。",  # 4
]


def _items():
    return [
        {"id": "frag-hall", "line_start": 1, "line_end": 1},
        {"id": "frag-kitchen", "line_start": 2, "line_end": 3},
        {"id": "frag-cellar", "line_start": 4, "line_end": 4},
    ]


def _assign(**overrides):
    base = {
        "frag-hall": {"item_id": "frag-hall", "dest_kind": "node", "dest_id": "hall"},
        "frag-kitchen": {"item_id": "frag-kitchen", "dest_kind": "node", "dest_id": "kitchen"},
        "frag-cellar": {"item_id": "frag-cellar", "dest_kind": "node", "dest_id": "cellar"},
    }
    base.update(overrides)
    return base


def _module(kitchen_subs=None, extra_nodes=None):
    return {
        "meta": {"id": "m", "title": "测试用"},
        "nodes": [
            {"id": "hall", "title": "门厅", "kp_text": "门厅积满灰尘，地板上有拖拽的痕迹。"},
            {
                "id": "kitchen",
                "title": "厨房",
                "kp_text": "厨房的水槽里泡着一只缺口的搪瓷碗。",
                "sub_nodes": kitchen_subs or [],
            },
            *(extra_nodes or []),
        ],
    }


# ── 断链修复：子节点继承祖先锚点 ────────────────────────


def test_sub_node_inherits_parent_anchor() -> None:
    """🔴 子节点没有自己的归组映射，但它是父片段拆出来的——必须继承，不能判成无锚点。

    这是实测里 25–57% 实体"没有锚点"的唯一成因。
    """
    subs = [{"id": "kitchen-sink", "title": "水槽", "kp_text": "水槽里泡着一只缺口的搪瓷碗"}]
    errors = check_source_traceability(_items(), _assign(), _module(kitchen_subs=subs), SOURCE)

    assert errors == [], f"子节点应继承父锚点，实际报了：{errors}"


def test_sub_node_inheriting_but_not_matching_still_fails() -> None:
    """继承来的锚点不是免死金牌——文本对不上源行照样硬失败。"""
    subs = [{"id": "kitchen-ufo", "title": "飞碟", "kp_text": "银色圆盘悬停于灶具正上方"}]
    errors = check_source_traceability(_items(), _assign(), _module(kitchen_subs=subs), SOURCE)

    assert any("kitchen-ufo" in e for e in errors)


# ── 无锚点 = 硬失败 ───────────────────────────────────


def test_entity_without_any_anchor_is_hard_failure() -> None:
    """顶层实体没有任何片段指派 → 它是凭空长出来的，硬失败。"""
    extra = [{"id": "attic", "title": "阁楼", "kp_text": "阁楼里堆着旧箱子"}]
    errors = check_source_traceability(_items(), _assign(), _module(extra_nodes=extra), SOURCE)

    assert any("attic" in e and "锚点" in e for e in errors)


# ── 逐字重合门 ────────────────────────────────────────


def test_entity_faithful_to_its_source_passes() -> None:
    errors = check_source_traceability(_items(), _assign(), _module(), SOURCE)

    assert errors == []


def test_entity_diverging_from_its_source_fails() -> None:
    """有锚点，但整段文本跟源行毫无逐字重合 → 编造。"""
    module = _module()
    module["nodes"][0]["kp_text"] = "海面漂来一只木桶，里头装着航海志。"
    errors = check_source_traceability(_items(), _assign(), module, SOURCE)

    assert any("hall" in e for e in errors)


def test_threshold_stays_a_floor_not_a_fidelity_judgement() -> None:
    """🔴 阈值必须显式、且必须**低**——它是兜底，不是忠实度判据。

    林中屋实测重合值是 3→11→18→30+ 的连续分布，**没有自然断点**：取 6 抓 2 个、
    取 8 抓 3 个、取 12 抓 7 个，全是任意的。把它调高等于开始误伤改写，而改写
    是组装的正常行为。真正抓错位/编造的是 AI 玩家试跑。
    """
    assert isinstance(MIN_TRACE_RUN, int)
    assert 2 <= MIN_TRACE_RUN <= 4, (
        "中文 3 字约等于一个词；调高它就不再是「连一个词都没对上」而是在判改写"
    )
