"""技能指向的对齐点（exec/12 #32 → exec/17 (A)）。

## 这个文件的口径变过一次，记下为什么

原始 bug（exec/12 #32）：《追书人》的节点把检定点标成「侦查」，裁决器按规范名
发起「侦察」，护栏做精确字符串比较 → 拦掉 → **玩家明说"我要过一下侦查"也不
掉骰子，而且静默无提示**。当时的修法是让护栏与执行层共用一张运行时同义词表。

exec/17 判定那个修法方向错了：**事后维护同义词字典是打地鼠**，换个模组、模型
换个措辞就又漏一个。正解是把对齐点从**运行时**挪到**组装期**——模组数据在
`normalize_module_skills` 里一次性归一成规则表 id，裁决器输出的也是 id，
运行时是纯集合比较，没有字符串匹配可言。

所以这里守的不变量没变，守的位置变了：

    模组里的野写法 ──组装期──→ skill_ids（id）
                                    ↕ 集合比较（护栏）
    裁决器输出 ────────────────→ skill_id（id）

`canonical_skill_name` / `match_key` 仍然保留：组装期的别名归一、以及运行时
「模型写了中文名」那条显式回退路径还在用它们（见 turn_executor 的
`keeper_skill_id_fallback`）。
"""

from __future__ import annotations

import pytest

from app.core.coc7.content import build_coc7_ruleset
from app.core.keeper.capabilities.skill_check.guard import filter_checks_against_module
from app.core.keeper.contract.module_loader import (
    KeeperTruth,
    ModuleCheck,
    ModuleMeta,
    ModuleNode,
    ScenarioModule,
)
from app.core.keeper.primitives.skills import canonical_skill_name, match_key
from scripts.module_probe.validate_module import (
    COC6_ATTRIBUTE_CHECKS,
    MULTI_SKILL_CHECKS,
    SAN_CHECK_WRITINGS,
    SKILL_ALIASES,
    resolve_check_skill,
)

_RULESET = build_coc7_ruleset()


def _module(*skill_ids: str) -> ScenarioModule:
    return ScenarioModule(
        meta=ModuleMeta(id="m", title="合成"),
        kp_truth=KeeperTruth(summary="真相"),
        player_intro="开场",
        nodes=[
            ModuleNode(
                id="study",
                title="书房",
                kp_text="材料",
                checks=[ModuleCheck(skill_ids=list(skill_ids))],
            )
        ],
    )


# ── 1. 组装期：野写法 → id ───────────────────────────


@pytest.mark.parametrize("variant", sorted(SKILL_ALIASES))
def test_every_alias_resolves_to_an_id(variant: str) -> None:
    """整张别名表逐条过一遍——补新别名时这条会自动覆盖到。"""
    kind, ids, display = resolve_check_skill(variant, _RULESET)
    assert kind == "skill"
    assert ids, f"别名 {variant!r} 没有归一到任何 id"
    assert display


@pytest.mark.parametrize("variant", sorted(COC6_ATTRIBUTE_CHECKS))
def test_coc6_leftovers_become_attribute_checks(variant: str) -> None:
    """🔴 灵感/知识在 COC7 里**不是技能**（Idea/Know → INT×5 / EDU×5）。

    5 个模组里共 11 条，是最大的一类脏数据。同义词表救不了它们——规则表里
    压根没有这两个技能，必须映射到属性 key。
    """
    _kind, ids, _display = resolve_check_skill(variant, _RULESET)
    assert ids == [COC6_ATTRIBUTE_CHECKS[variant]]
    assert ids[0] in {a.key for a in _RULESET.attributes}


@pytest.mark.parametrize("variant", sorted(SAN_CHECK_WRITINGS))
def test_san_writings_become_san_kind_with_no_skill(variant: str) -> None:
    """理智检定该走 san_checks，不指向任何技能。"""
    kind, ids, display = resolve_check_skill(variant, _RULESET)
    assert kind == "san"
    assert ids == []
    assert display == "理智检定"


@pytest.mark.parametrize("variant", sorted(MULTI_SKILL_CHECKS))
def test_multi_skill_checks_keep_every_option(variant: str) -> None:
    """多选检定点（"话术/魅惑/信用"）保留全部候选，护栏任一命中即放行。"""
    _kind, ids, _display = resolve_check_skill(variant, _RULESET)
    assert ids == MULTI_SKILL_CHECKS[variant]
    assert len(ids) > 1


def test_unresolvable_writing_yields_no_id_instead_of_guessing() -> None:
    """🔴 解析不出就是解析不出——不猜。

    `check_skills` 会因为 `skill_ids` 为空而阻断产出，脏数据进不了
    structured.json。此前它只报错不拦，于是 43 条脏数据照样能产出可主持的
    模组，运行时再靠字符串匹配去猜，猜不中就静默丢检定。
    """
    kind, ids, display = resolve_check_skill("量子纠缠学", _RULESET)
    assert kind == "skill"
    assert ids == []
    assert display == "量子纠缠学"  # 原文留着，报错信息要能指出是哪一条


# ── 2. 运行时：id vs id ─────────────────────────────


def test_guard_lets_through_the_annotated_id() -> None:
    allowed, issues = filter_checks_against_module(
        _module("spot-hidden"), ["spot-hidden"], current_scene="书房", current_node_id="study"
    )
    assert allowed == ["spot-hidden"]
    assert issues == []


def test_guard_still_withholds_reveal_rights_from_an_unannotated_skill() -> None:
    """护栏不能放水成摆设——没标注的技能拿不到揭示权（设计 02 第一层）。

    🔴 **2026-08-15：函数的返回值契约没动，动的是调用方怎么用它。** 原来这条
    叫 `..._still_blocks_...`，因为返回的空列表当时意味着"这条检定整个丢弃"；
    现在它意味着"照掷，但 `reveals` 为空"。**没标注 = 挖不出模组真相**这个
    不变式一点没松，松的只是"顺带没收玩家掷骰的权利"那部分。
    """
    allowed, issues = filter_checks_against_module(
        _module("spot-hidden"), ["library-use"], current_scene="书房", current_node_id="study"
    )
    assert allowed == []
    assert issues and "揭不开模组事实" in issues[0]


def test_guard_accepts_any_option_of_a_multi_skill_check() -> None:
    module = _module("fast-talk", "charm", "credit-rating")
    for option in ("fast-talk", "charm", "credit-rating"):
        allowed, _ = filter_checks_against_module(
            module, [option], current_scene="书房", current_node_id="study"
        )
        assert allowed == [option], f"多选检定点应放行 {option!r}"


def test_guard_does_not_block_when_module_has_no_machine_readable_ids() -> None:
    """未归一的老模组（skill_ids 空）→ 不挡。

    **显式降级**：没有机器可读的白名单就没有可执行的限制。不是兜底猜测——
    代码不会退回去按名字比对。
    """
    allowed, issues = filter_checks_against_module(
        _module(), ["library-use"], current_scene="书房", current_node_id="study"
    )
    assert allowed == ["library-use"]
    assert issues == []


# ── 3. 归一函数本身（组装期与运行时回退路径仍在用）──────────


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
