"""主体与权限（exec/14 P2）。

两条主线：
1. **守秘人这条路径必须逐字节不变**——全权限下 sanitize/authorize/schema 构造
   全是恒等操作。这是 P2「重构不改行为」的硬要求。
2. **受限主体的越权动作无法表达**，且执行边界还会再拦一道（纵深防御）。
"""

from __future__ import annotations

from pathlib import Path

from app.core.keeper.capabilities.health.schema import HpChange
from app.core.keeper.decision import KeeperDecision, StateUpdate
from app.core.keeper.module_loader import (
    ModuleFact,
    load_module,
    render_for_subject,
    render_full,
)
from app.core.keeper.subject import (
    ALL_CAPABILITIES,
    KEEPER,
    Capability,
    Subject,
    authorize_decision,
    build_decision_model,
    npc_subject,
    sanitize_decision,
)

FIXTURE = Path(__file__).parent / "fixtures" / "keeper_module.json"


def _full_decision() -> KeeperDecision:
    return KeeperDecision(
        thinking="理由",
        hp_changes=[HpChange(delta=-2, reason="摔了一跤")],
        state_updates=[StateUpdate(key="当前场景", value="门厅")],
        current_node_id="hall",
        opening_complete=True,
        narration_guidance="给方向",
    )


# ── 守秘人路径：恒等 ─────────────────────────────────────────────


def test_keeper_holds_every_capability() -> None:
    assert KEEPER.capabilities == ALL_CAPABILITIES
    assert KEEPER.sees_meta is True
    assert KEEPER.knows("任何 fact id") is True


def test_keeper_decision_passes_authorization_untouched() -> None:
    decision = _full_decision()
    assert authorize_decision(KEEPER, decision) == []
    # 原样返回同一个对象——连一次拷贝都不该发生
    assert sanitize_decision(KEEPER, decision) is decision


def test_keeper_schema_is_the_original_class() -> None:
    """全权限时返回 KeeperDecision 本身，不是等价副本。"""
    assert build_decision_model(ALL_CAPABILITIES) is KeeperDecision


def test_keeper_view_is_render_full_itself() -> None:
    module = load_module(FIXTURE)
    assert render_for_subject(module, sees_meta=True, knows=KEEPER.knows) == render_full(module)


# ── 受限主体：越权无法表达，且执行边界再拦一道 ──────────────────


def test_restricted_schema_drops_forbidden_fields() -> None:
    """🔴 权限的载体是"调用前给它的 schema"——越权动作**无法表达**。"""
    model = build_decision_model(frozenset({Capability.UPDATE_STATE}))
    assert "state_updates" in model.model_fields
    assert "hp_changes" not in model.model_fields
    assert "checks" not in model.model_fields
    # 非动作字段（说话/思考/分类）任何主体都保留
    assert "narration_guidance" in model.model_fields
    assert "thinking" in model.model_fields


def test_restricted_schema_ignores_extra_fields_instead_of_raising() -> None:
    """LLM 多吐一个越权字段时忽略即可，不该整轮炸掉（沿用 extra='ignore'）。"""
    model = build_decision_model(frozenset({Capability.UPDATE_STATE}))
    parsed = model.model_validate({"hp_changes": [{"delta": -99}], "thinking": "越权"}).model_dump()
    # 越权字段连进都进不来（不是"进来了但被忽略"）
    assert "hp_changes" not in parsed
    assert parsed["thinking"] == "越权"


def test_authorization_reports_each_violation() -> None:
    reader = Subject(id="npc-butler", kind="npc")
    violations = authorize_decision(reader, _full_decision())
    assert len(violations) == 4  # hp / state / scene / phase
    assert any("adjust_hp" in v for v in violations)


def test_sanitize_clears_only_forbidden_fields() -> None:
    subject = Subject(id="npc", kind="npc", capabilities=frozenset({Capability.UPDATE_STATE}))
    cleaned = sanitize_decision(subject, _full_decision())
    assert cleaned.hp_changes == []
    assert cleaned.current_node_id is None
    assert cleaned.opening_complete is False
    # 有权做的照留，思考/指引这类非动作字段不受影响
    assert cleaned.state_updates[0].key == "当前场景"
    assert cleaned.narration_guidance == "给方向"


def test_default_valued_fields_are_not_treated_as_actions() -> None:
    """空列表 / None / False 不是一次动作，不该报越权。"""
    assert (
        authorize_decision(Subject(id="npc", kind="npc"), KeeperDecision(thinking="只是想想")) == []
    )


# ── 视图：元层对任何虚构内主体永不可见 ────────────────────────


def test_npc_view_hides_meta_layer() -> None:
    module = load_module(FIXTURE)
    npc = npc_subject("butler", frozenset())
    view = render_for_subject(module, sees_meta=npc.sees_meta, knows=npc.knows)

    assert module.kp_truth.summary not in view
    for fact in module.kp_truth.key_facts:
        assert fact not in view
    for ending in module.endings:
        assert ending.text not in view
    for node in module.nodes:
        assert node.kp_text not in view
    for who in module.npcs:
        if who.kp_notes:
            assert who.kp_notes not in view


def test_npc_view_only_shows_facts_it_knows() -> None:
    module = load_module(FIXTURE)
    module.facts.append(ModuleFact(id="fact-001", text="管家昨晚看见有人翻窗"))
    module.facts.append(ModuleFact(id="fact-002", text="地窖里藏着一具尸体"))
    knowing = npc_subject("butler", frozenset({"fact-001"}))
    view = render_for_subject(module, sees_meta=knowing.sees_meta, knows=knowing.knows)
    assert "管家昨晚看见有人翻窗" in view
    assert "地窖里藏着一具尸体" not in view


def test_npc_view_never_shows_meta_facts_even_if_known() -> None:
    """就算 known_fact_ids 里混进了 meta 事实，也不能渲染出来——层次轴优先。"""
    module = load_module(FIXTURE)
    module.facts.append(
        ModuleFact(id="fact-meta", text="真凶其实是管家本人", kind="truth", tier="meta")
    )
    subject = npc_subject("butler", frozenset({"fact-meta"}))
    view = render_for_subject(module, sees_meta=subject.sees_meta, knows=subject.knows)
    assert "真凶其实是管家本人" not in view
