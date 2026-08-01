"""能力注册表本身的验收（exec/27 阶段 2）。

这份用例守的不是某个能力的业务，而是**组装机制**：注册进来的四样东西有没有
真的被骨架用上、顺序稳不稳、忘了接线会不会静默。
"""

from __future__ import annotations

from pathlib import Path

from app.core.coc7_content import build_coc7_ruleset
from app.core.keeper import capabilities as registry_pkg
from app.core.keeper.decision import KeeperDecision
from app.core.keeper.module_loader import load_module
from app.core.keeper.prompts import build_adjudicator_instructions
from app.core.keeper.subject import DECISION_FIELD_CAPABILITIES
from app.core.keeper.turn_executor import _SKELETON_STEP_ORDERS

_MODULE = load_module(str(Path(__file__).parent / "fixtures" / "keeper_module.json"))
_RULESET = build_coc7_ruleset()


def test_every_registered_schema_is_actually_inherited() -> None:
    """🔴 schema 片段是唯一需要在 `decision.py` 手写一行的钩子。

    没有这条断言，"注册了但忘了继承"就是**静默失败**：模型照常被教着写
    `hp_changes`，而 schema 里没有这个字段 → pydantic 按 extra='ignore' 丢掉
    → 血扣不上、日志里什么都没有。这正是本项目反复记的那类 bug。
    """
    for fragment in registry_pkg.registered_schemas():
        assert issubclass(KeeperDecision, fragment), (
            f"{fragment.__name__} 注册了但 KeeperDecision 没有继承它——"
            "在 app/core/keeper/decision.py 的基类列表里加上"
        )
        for name in fragment.model_fields:
            assert name in KeeperDecision.model_fields


def test_field_capabilities_are_merged_into_the_permission_table() -> None:
    """能力自带的权限声明要真的进 `subject` 的那张表，否则受限主体的越权
    字段既不会从 schema 里被摘掉，也不会在执行边界被拦。"""
    for capability in registry_pkg.CAPABILITIES:
        for field, needed in capability.field_capabilities.items():
            assert DECISION_FIELD_CAPABILITIES[field] is needed


def test_prompt_blocks_land_in_the_adjudicator_instructions() -> None:
    """注册的 prompt 块必须出现在成品里，且**按 order 归位**。"""
    text = build_adjudicator_instructions(_MODULE, _RULESET)
    for capability in registry_pkg.CAPABILITIES:
        for block in capability.prompt_blocks:
            assert block.text in text, f"{capability.name} 的 prompt 块没进裁决指令"

    # health 的规则 3b（order 3.4）必须夹在骨架的规则 3 和 4 之间
    assert text.index("\n3. ") < text.index("\n3b. ") < text.index("\n4. ")


def test_situation_blocks_render_nothing_when_empty() -> None:
    """没有内容时整块（连标题）不渲染——没记过账的对局，局面块与切分前逐字一致。"""
    assert registry_pkg.situation_blocks(_MODULE, None) == []
    assert registry_pkg.situation_blocks(_MODULE, {"当前场景": "书房"}) == []


def test_hooks_are_sorted_and_do_not_collide_with_skeleton_steps() -> None:
    """order 的语义：能力钩子与骨架剩下的步骤共用一条数轴，重号就分不出先后。"""
    orders = [hook.order for hook in registry_pkg.executors()]
    assert orders == sorted(orders)
    assert len(set(orders)) == len(orders)
    assert not set(orders) & set(_SKELETON_STEP_ORDERS.values())
