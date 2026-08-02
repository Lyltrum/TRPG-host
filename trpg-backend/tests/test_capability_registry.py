"""能力注册表本身的验收（exec/27 阶段 2）。

这份用例守的不是某个能力的业务，而是**组装机制**：注册进来的四样东西有没有
真的被骨架用上、顺序稳不稳、忘了接线会不会静默。
"""

from __future__ import annotations

from pathlib import Path

from app.core.coc7_content import build_coc7_ruleset
from app.core.keeper import capabilities as registry_pkg
from app.core.keeper.access.subject import DECISION_FIELD_CAPABILITIES
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.contract.registry import SituationContext
from app.core.keeper.narration.prompts import build_adjudicator_instructions

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


def test_situation_blocks_skip_capabilities_with_nothing_to_say() -> None:
    """render 返回空串时整块（连标题）不渲染——否则局面块里会多出空标题。

    注意不是"状态空就什么都不渲染"：`agenda` 在开局就有话说（它要列出**尚未
    发生**的事件），而 `health` 要等真有 NPC 掉过血才出现。两种都对。
    """
    for keeper_state in (None, {"当前场景": "书房"}):
        rendered = registry_pkg.situation_blocks(_MODULE, keeper_state)
        expected = [
            block
            for capability in registry_pkg.CAPABILITIES
            for block in capability.situations
            if block.render(SituationContext(_MODULE, keeper_state))
        ]
        assert len(rendered) == len(expected)
        assert [order for order, _ in rendered] == sorted(order for order, _ in rendered)
        for _, text in rendered:
            assert not text.endswith("\n\n\n"), "空内容却渲染了标题"


def test_audit_fields_are_merged_from_every_capability() -> None:
    """🔴 漏了 audit 不报错，只是那片能力在日志里**隐身**——线上排查时看不出
    它本轮做没做事。所以这条断言盯的是"注册了就一定进得去"。"""
    decision = KeeperDecision(thinking="砍中", narration_guidance="写打斗")
    merged = registry_pkg.audit_fields(decision)
    for capability in registry_pkg.CAPABILITIES:
        if capability.audit is None:
            continue
        for key in capability.audit(decision):
            assert key in merged


def test_hooks_are_sorted_and_never_share_an_order() -> None:
    """order 的语义：执行顺序决定副作用先后与执行报告行序，重号就分不出先后。

    骨架侧那份步骤表已经没有了——`execute_side_effects` 现在完全由注册表驱动
    （exec/27 阶段 3 收尾）。
    """
    orders = [hook.order for hook in registry_pkg.executors()]
    assert orders == sorted(orders)
    assert len(set(orders)) == len(orders)


def test_no_rule_or_example_line_is_emitted_twice() -> None:
    """🔴 切一片能力 = 能力里加一份 + **骨架里删一份**。忘了删就会重复。

    实测（切 `progression` 时）：规则 10 与两行输出示例在成品里各出现了两次，
    而组装机制本身一切正常——它只负责按 order 拼，不知道两段说的是同一件事。
    当时是磁带漂移断言抓到的，但那条只覆盖一个模组一轮对话；这条直接按结构查，
    任何一片能力切错都躲不过。
    """
    import re

    text = build_adjudicator_instructions(_MODULE, _RULESET)
    labels = re.findall(r"^(\d+[a-z]?)\. ", text, flags=re.MULTILINE)
    duplicated = sorted({label for label in labels if labels.count(label) > 1})
    assert not duplicated, f"裁决规则编号重复：{duplicated}——骨架里那份忘了删"

    keys = re.findall(r'^\s+"(\w+)":', text, flags=re.MULTILINE)
    dup_keys = sorted({k for k in keys if keys.count(k) > 1})
    assert not dup_keys, f"输出格式示例里字段重复：{dup_keys}"


def test_the_scene_fact_is_published_before_it_is_consumed() -> None:
    """🔴 `TurnFacts` 是一条**有方向**的契约：`world_state` 写、`movement` 读。

    它靠 order 保证——写的那片必须先跑。这条断言是那个保证的全部强度：把两片的
    order 调换，`movement` 就会读到上一轮遗留的 None，`exec/19 #48` 的清空逻辑
    静默失效（人站在屋外，护栏还拿旧节点卡他），而**没有任何东西会报错**。
    """
    order = {c.name: c.executors[0].order for c in registry_pkg.CAPABILITIES if c.executors}
    assert order["world_state"] < order["movement"], (
        "world_state 必须先于 movement 执行：前者 publish 「当前场景」，后者据此"
        "决定要不要清空节点指针"
    )


def test_every_executor_hook_takes_the_turn_facts() -> None:
    """签名统一：加一片能力时不必猜"要不要收那个参数"。"""
    import inspect

    for capability in registry_pkg.CAPABILITIES:
        for hook in capability.executors:
            params = list(inspect.signature(hook.run).parameters)
            assert len(params) == 3, f"{capability.name} 的执行钩子签名不是 (deps, decision, facts)"


def test_every_pending_kind_has_exactly_one_settler() -> None:
    """🔴 发起与结算必须**两头对齐**。

    `pending` 钩子负责发起、`settlers` 负责结算，中间隔着数据库里的待掷队列。
    只做了一半的话：新检定发得出去、结算时找不到认领者。此前结算是一条写死的
    if/else 且带 else 兜底——那种情况下新检定会被**静默当成 SAN 检定结算**，
    掷骰数字照样出现在玩家屏幕上，没有任何东西会红。
    """
    kinds = [h.kind for c in registry_pkg.CAPABILITIES for h in c.settlers]
    assert len(set(kinds)) == len(kinds), f"同一种 kind 被多片能力认领：{kinds}"
    # 有 pending 钩子的能力必须也有 settler（反之亦然）
    for capability in registry_pkg.CAPABILITIES:
        assert bool(capability.pendings) == bool(capability.settlers), (
            f"{capability.name} 只做了两段式掷骰的一半"
        )


def test_an_unclaimed_kind_raises_instead_of_falling_through() -> None:
    """没人认领就炸——不要有 else 兜底。"""
    import pytest

    with pytest.raises(KeyError):
        registry_pkg.settler_for("no-such-kind")
