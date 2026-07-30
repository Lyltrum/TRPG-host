"""事实表 schema 与校验（exec/15，P1.2）。

用合成模组构造，不依赖 `模组资料/`（真实模组 gitignore，CI 里没有）。
P1.2 只动 IR 与校验，**不改 keeper 运行时行为**——行为不变由
`tests/test_keeper_replay.py` 的磁带回放守着。
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.core.keeper.module_loader import (
    KeeperTruth,
    ModuleCheck,
    ModuleFact,
    ModuleMeta,
    ModuleNode,
    ModuleNpc,
    ScenarioModule,
    load_module,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.module_probe.validate_module import check_facts  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "keeper_module.json"


def _module(
    *,
    facts: list[ModuleFact] | None = None,
    nodes: list[ModuleNode] | None = None,
    npcs: list[ModuleNpc] | None = None,
) -> ScenarioModule:
    return ScenarioModule(
        meta=ModuleMeta(id="m", title="合成模组"),
        kp_truth=KeeperTruth(summary="真相"),
        player_intro="开场",
        nodes=nodes or [],
        npcs=npcs or [],
        facts=facts or [],
    )


def _node(node_id: str, **kw) -> ModuleNode:
    return ModuleNode(id=node_id, title=node_id, kp_text="材料", **kw)


# --- schema 形状 ---


def test_legacy_module_without_facts_still_loads() -> None:
    """向后兼容是硬要求：尚未迁移的模组必须照常加载、facts 为空。"""
    module = load_module(FIXTURE)
    assert module.facts == []
    assert module.diegetic_fact_ids() == set()


def test_fact_defaults_are_diegetic_clue() -> None:
    fact = ModuleFact(id="fact-001", text="地毯上有半干的泥脚印")
    assert fact.tier == "diegetic"
    assert fact.kind == "clue"


def test_fact_by_id_and_diegetic_filter() -> None:
    module = _module(
        facts=[
            ModuleFact(id="fact-001", text="线索"),
            ModuleFact(id="fact-002", text="结局条件", tier="meta"),
        ]
    )
    assert module.fact_by_id("fact-001") is not None
    assert module.fact_by_id("nope") is None
    # meta 不进 view 候选集：元层对任何虚构内主体永不可见
    assert module.diegetic_fact_ids() == {"fact-001"}


def test_one_fact_can_have_multiple_reveal_paths() -> None:
    """🔴 事实是模组级的表，同一条可以被多处揭开（COC 多路径线索）。

    实测依据：同一条 on_success 在多个检定下重复出现（神秘渡轮 71→57）。
    """
    module = _module(
        facts=[ModuleFact(id="fact-001", text="同一条线索")],
        nodes=[
            _node("n1", checks=[ModuleCheck(skill="侦察", reveals=["fact-001"])]),
            _node("n2", checks=[ModuleCheck(skill="图书馆使用", reveals=["fact-001"])]),
        ],
        npcs=[ModuleNpc(id="npc1", name="管家", knows=["fact-001"])],
    )
    assert check_facts(module) == []


# --- 校验 ---


def test_empty_facts_passes() -> None:
    assert check_facts(_module()) == []


def test_dangling_reveals_is_error() -> None:
    module = _module(
        facts=[ModuleFact(id="fact-001", text="线索")],
        nodes=[_node("n1", checks=[ModuleCheck(skill="侦察", reveals=["fact-999"])])],
    )
    errors = check_facts(module)
    assert any("悬空" in e and "fact-999" in e for e in errors)


def test_dangling_npc_knows_is_error() -> None:
    module = _module(
        facts=[ModuleFact(id="fact-001", text="线索")],
        nodes=[_node("n1", reveals=["fact-001"])],
        npcs=[ModuleNpc(id="npc1", name="管家", knows=["fact-404"])],
    )
    assert any("npc" in e and "fact-404" in e for e in check_facts(module))


def test_dead_clue_is_error() -> None:
    """虚构内事实必须至少有一条揭开路径，否则玩家永远拿不到。"""
    module = _module(facts=[ModuleFact(id="fact-001", text="谁也拿不到的线索")])
    assert any("死线索" in e for e in check_facts(module))


def test_meta_fact_needs_no_reveal_path() -> None:
    """meta 不可挣得，所以没有揭开路径是正常的，不该报死线索。"""
    module = _module(facts=[ModuleFact(id="fact-001", text="结局条件", tier="meta")])
    assert check_facts(module) == []


def test_meta_fact_in_reveals_is_error() -> None:
    """把主持指导误当线索挂到检定上——迁移时最可能犯的错。"""
    module = _module(
        facts=[ModuleFact(id="fact-001", text="结局条件", tier="meta")],
        nodes=[_node("n1", checks=[ModuleCheck(skill="侦察", reveals=["fact-001"])])],
    )
    assert any("meta 层" in e for e in check_facts(module))


def test_duplicate_fact_id_is_error() -> None:
    module = _module(
        facts=[
            ModuleFact(id="fact-001", text="甲"),
            ModuleFact(id="fact-001", text="乙"),
        ],
        nodes=[_node("n1", reveals=["fact-001"])],
    )
    assert any("重复" in e for e in check_facts(module))


def test_illegal_tier_or_kind_is_error() -> None:
    module = _module(
        facts=[ModuleFact(id="fact-001", text="线索", tier="secret", kind="rumor")],
        nodes=[_node("n1", reveals=["fact-001"])],
    )
    errors = check_facts(module)
    assert any("tier 非法" in e for e in errors)
    assert any("kind 非法" in e for e in errors)


def test_blank_fact_text_is_error() -> None:
    module = _module(
        facts=[ModuleFact(id="fact-001", text="   ")],
        nodes=[_node("n1", reveals=["fact-001"])],
    )
    assert any("text 为空" in e for e in check_facts(module))
