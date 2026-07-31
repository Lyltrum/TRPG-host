"""技能名归一：护栏与执行层必须同一口径（exec/12 #32）。

真人实测撞见的 bug：《追书人》的节点把检定点标成「侦查」，裁决器按被教对的
规范名发起「侦察」，护栏做精确字符串比较 → 拦掉 → **玩家明说"我要过一下
侦查"也不掉骰子，而且静默无提示**。
"""

from __future__ import annotations

from app.core.keeper.check_guard import filter_checks_against_module
from app.core.keeper.module_loader import (
    KeeperTruth,
    ModuleCheck,
    ModuleMeta,
    ModuleNode,
    ScenarioModule,
)
from app.core.keeper.skill_names import SKILL_SYNONYMS, canonical_skill_name, match_key


def _module(annotated_skill: str) -> ScenarioModule:
    return ScenarioModule(
        meta=ModuleMeta(id="m", title="合成"),
        kp_truth=KeeperTruth(summary="真相"),
        player_intro="开场",
        nodes=[
            ModuleNode(
                id="study",
                title="书房",
                kp_text="材料",
                checks=[ModuleCheck(skill=annotated_skill)],
            )
        ],
    )


def test_canonical_name_maps_known_variants() -> None:
    assert canonical_skill_name("侦查") == "侦察"
    assert canonical_skill_name("观察") == "侦察"
    assert canonical_skill_name("闪躲") == "闪避"


def test_canonical_name_leaves_unknown_names_alone() -> None:
    """只做确定性替换，不模糊匹配——瞎猜大类前缀已经踩过坑（exec/09 #5）。"""
    assert canonical_skill_name("驾驶") == "驾驶"
    assert canonical_skill_name("  侦察 ") == "侦察"


def test_match_key_ignores_case_and_spaces() -> None:
    assert match_key("Spot Hidden") == match_key("spothidden")


# ── 🔴 不变量：护栏不该拦住执行层能解析的技能名 ──────────────


def test_guard_lets_through_a_synonym_of_the_annotated_skill() -> None:
    """模组写「侦查」、裁决器发「侦察」——必须放行。

    修复前这里会被拦掉，且只进 issue 日志，玩家侧完全静默。
    """
    allowed, issues = filter_checks_against_module(
        _module("侦查"), ["侦察"], current_scene="书房", current_node_id="study"
    )
    assert allowed == ["侦察"]
    assert issues == []


def test_guard_still_blocks_a_genuinely_unannotated_skill() -> None:
    """归一不能把护栏放水成摆设——没标注的技能照样拦（设计 02 第一层）。"""
    allowed, issues = filter_checks_against_module(
        _module("侦查"), ["图书馆使用"], current_scene="书房", current_node_id="study"
    )
    assert allowed == []
    assert issues and "未发起" in issues[0]


def test_every_synonym_is_accepted_by_the_guard() -> None:
    """整张表逐条过一遍——补新同义词时这条会自动覆盖到。"""
    for variant, canonical in SKILL_SYNONYMS.items():
        allowed, _ = filter_checks_against_module(
            _module(variant), [canonical], current_scene="书房", current_node_id="study"
        )
        assert allowed == [canonical], f"模组写 {variant!r}、裁决发 {canonical!r} 被拦了"
        # 反向也要成立：模组写规范名、裁决发口语名
        allowed, _ = filter_checks_against_module(
            _module(canonical), [variant], current_scene="书房", current_node_id="study"
        )
        assert allowed == [variant], f"模组写 {canonical!r}、裁决发 {variant!r} 被拦了"
