"""分片自修：实体级错误只重吐那一个实体（`exec/29` 第 3 步落地记录）。

## 为什么改成分片

整份重吐**在真实体量的模组上结构性地吐不完**。实测（林中屋，端到端跑完之后）：

    产物 JSON        25407 字符
    自修响应被截在    21916 字符
    三次尝试          全部断在同一位置

这不是偶发，是必然——模组越大越必然。而**自修是拒绝率的最后一道缓冲**：它坏了，
拒绝率就直接等于首次校验的失败率。

## 分界线

| 错误类别 | 作用域 | 修法 |
|---|---|---|
| `skill` / `trace` / `numeric` | 挂在**一个实体**上 | 只重吐那个实体，**输出有界** |
| `leak` / `structure` / `facts` / `schema` | 跨越整份产物 | 只能整份修 |
| `ref` | — | 走机械修补，不上 LLM |

`thin_slot` / `orphan` / `secret_public` 走的是回灌阶段 1（`needs_stage1_repair`），
不在这条路上。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.module_probe import assemble  # noqa: E402
from scripts.module_probe.validate_module import ValidationReport  # noqa: E402


def _module() -> dict:
    return {
        "meta": {"id": "m", "title": "t"},
        "nodes": [
            {
                "id": "house",
                "title": "宅邸",
                "kp_text": "门厅",
                "sub_nodes": [{"id": "cellar", "title": "地窖", "kp_text": "木梯"}],
            }
        ],
        "npcs": [{"id": "keeper", "name": "看守"}],
        "endings": [{"id": "done", "title": "收束", "text": "结束"}],
        "agenda": [{"id": "night", "trigger": "入夜", "kp_text": "滴水声"}],
    }


# ── 分界线本身 ────────────────────────────────────────


def test_entity_scoped_categories_are_grouped_by_entity() -> None:
    report = ValidationReport(
        ok=False,
        schema_ok=True,
        skill_errors=["node 'house' checks[0] 未归一到技能 id（原文 'INT×4'）"],
        numeric_errors=[
            "node 'house' 的数值 '701d6' 在原文里找不到——疑似凭空生成",
            "npc 'keeper' 的数值 '9d9' 在原文里找不到——疑似凭空生成",
        ],
    )

    grouped = assemble.entity_scoped_errors(report)

    assert set(grouped) == {"house", "keeper"}
    assert len(grouped["house"]) == 2


def test_cross_entity_categories_are_not_grouped() -> None:
    """🔴 `leak` 说的是「真相漏进了玩家可见字段」，那不属于任何单个实体。

    误把它归成实体级，就会去重吐一个跟问题无关的实体，而真问题原地不动。
    """
    report = ValidationReport(
        ok=False,
        schema_ok=True,
        leak_errors=["真相关键词 '精神崩溃' 出现在玩家可见字段 player_intro"],
        structure_errors=["源片段带结局信号但 endings 为空"],
    )

    assert assemble.entity_scoped_errors(report) == {}


# ── 定位与替换 ────────────────────────────────────────


def test_entity_is_found_at_any_depth() -> None:
    """子节点也要能被定位——它们正是溯源/数值错误的高发区。"""
    module = _module()

    for eid in ("house", "cellar", "keeper", "done", "night"):
        spot = assemble.find_entity(module, eid)
        assert spot is not None, eid
        arr, idx = spot
        assert arr[idx]["id"] == eid


def test_missing_entity_returns_none_instead_of_guessing() -> None:
    assert assemble.find_entity(_module(), "nowhere") is None


def test_replacement_lands_in_the_real_tree_not_a_copy() -> None:
    """定位返回的必须是**原地的**列表与下标，否则修完的实体丢在副本里。"""
    module = _module()
    spot = assemble.find_entity(module, "cellar")
    assert spot is not None
    arr, idx = spot

    arr[idx] = {"id": "cellar", "title": "地窖", "kp_text": "第四级断了"}

    assert module["nodes"][0]["sub_nodes"][0]["kp_text"] == "第四级断了"
