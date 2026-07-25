"""设计 02：模组 checks 护栏。"""

from app.core.keeper.check_guard import (
    collect_module_check_skills,
    filter_checks_against_module,
    find_node_for_scene,
)
from app.core.keeper.module_loader import (
    KeeperTruth,
    ModuleCheck,
    ModuleMeta,
    ModuleNode,
    ScenarioModule,
)


def _mod() -> ScenarioModule:
    return ScenarioModule(
        meta=ModuleMeta(id="t", title="测"),
        kp_truth=KeeperTruth(summary="x"),
        player_intro="intro",
        nodes=[
            ModuleNode(
                id="house",
                title="科比特宅邸",
                kp_text="…",
                checks=[
                    ModuleCheck(skill="侦查", on_success="看见"),
                    ModuleCheck(skill="聆听", on_success="听见"),
                ],
            ),
            ModuleNode(id="street", title="街道", kp_text="…", checks=[]),
        ],
    )


def test_collect_skills() -> None:
    skills = collect_module_check_skills(_mod())
    assert "侦查" in skills or "侦查".lower() in skills
    assert any("侦" in s for s in skills)


def test_filter_blocks_improvised_when_node_has_checks() -> None:
    kept, issues = filter_checks_against_module(
        _mod(),
        ["侦查", "克苏鲁神话"],
        current_scene="科比特宅邸",
    )
    assert kept == ["侦查"]
    assert any("克苏鲁神话" in i for i in issues)


def test_filter_allows_when_node_has_no_checks() -> None:
    kept, issues = filter_checks_against_module(
        _mod(),
        ["话术"],
        current_scene="街道",
    )
    assert kept == ["话术"]
    assert issues == []


def test_find_node() -> None:
    m = _mod()
    assert find_node_for_scene(m, "科比特宅邸") is not None
    assert find_node_for_scene(m, "house") is not None
