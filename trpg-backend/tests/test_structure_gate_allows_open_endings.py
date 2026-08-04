"""结构完整性门：不许吞进 kp_truth，但**不许强求 endings 非空**（`exec/29`）。

## 这道门原来问错了问题

旧口径是「源片段的 what_kind 里有『结局』信号 → `endings[]` 不得为空」。它把
两件事焊死了：**这段材料没丢** 和 **这段材料落在 endings 里**。

实测林中屋证明第二件不该成立：原文那一行是「模组尾声，提供战役延续的可能性」，
是给守秘人的收尾材料，不是玩家能走到的收束点。旧口径连同阶段 1 的 prompt 一起
把它顶成了 `endings[0]`（归组理由原话：「尾声是模组结局，属于 ending」），于是
这个模组**永远收束不了**——而下游试跑只会报「没走到结局」，把账算在模组头上。

**伪造一条结局比承认没有结局贵得多**：伪造出来的那条会一路骗到试跑判据。

## 门现在只守一件事

rule C 那行括号里本来就写清楚了要防什么：**不可吞进 kp_truth**。信息进了守秘人
真相块，谁也用不上，等于蒸发。落在 ending 还是 kp_guidance 还是终局那个 node，
是语义判断，机械层不替 LLM 选。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.module_probe.validate_module import check_structure_integrity  # noqa: E402


def _items(what_kind: str) -> list[dict]:
    return [{"id": "epilogue", "what_kind_of_thing": what_kind, "summary": "s"}]


# ── 门放行什么 ────────────────────────────────────────


def test_ending_material_routed_to_guidance_is_accepted() -> None:
    """🔴 核心回归：尾声进 kp_guidance 且 endings[] 为空，这是**对的**。

    旧门在这里报错，逼出一条假结局。
    """
    errors = check_structure_integrity(
        _items("关于模组尾声的叙述，提供战役延续的可能性"),
        {"epilogue": {"dest_kind": "kp_guidance", "dest_id": "epilogue"}},
    )

    assert errors == []


def test_real_ending_routed_to_endings_is_accepted() -> None:
    errors = check_structure_integrity(
        _items("结局描述"),
        {"epilogue": {"dest_kind": "ending", "dest_id": "epilogue"}},
    )

    assert errors == []


def test_final_scene_routed_to_a_node_is_accepted() -> None:
    """终局那一幕做成可到达节点也合法——机械层不预设专属路由表。"""
    errors = check_structure_integrity(
        _items("结局场景"),
        {"epilogue": {"dest_kind": "node", "dest_id": "final-scene"}},
    )

    assert errors == []


def test_items_without_the_signal_are_not_examined() -> None:
    errors = check_structure_integrity(
        _items("某个房间的陈设描述"),
        {"epilogue": {"dest_kind": "kp_truth", "dest_id": "truth"}},
    )

    assert errors == []


# ── 门拦什么 ──────────────────────────────────────────


def test_ending_material_swallowed_into_kp_truth_is_rejected() -> None:
    """这才是这道门存在的理由：进了 kp_truth 就是谁也用不上。"""
    errors = check_structure_integrity(
        _items("结局描述"),
        {"epilogue": {"dest_kind": "kp_truth", "dest_id": "truth"}},
    )

    assert len(errors) == 1
    assert "kp_truth" in errors[0] and "epilogue" in errors[0]


def test_agenda_material_swallowed_into_kp_truth_is_rejected() -> None:
    """议程信号同口径——时间压力被吞掉，世界就不会自己推进了。"""
    errors = check_structure_integrity(
        [{"id": "tonight", "what_kind_of_thing": "今晚会发生的事，时间压力"}],
        {"tonight": {"dest_kind": "kp_truth", "dest_id": "truth"}},
    )

    assert len(errors) == 1
    assert "tonight" in errors[0]


def test_unassigned_signal_item_is_left_to_the_orphan_gate() -> None:
    """没出现在归组映射里 = 孤儿，那是 `check_orphans` 的活，这里不重复报。

    两道门报同一件事，修的人会以为是两个问题。
    """
    assert check_structure_integrity(_items("结局描述"), {}) == []
