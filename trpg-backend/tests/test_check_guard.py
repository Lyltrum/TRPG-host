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
                    ModuleCheck(skill_ids=["spot-hidden"], skill="侦察", on_success="看见"),
                    ModuleCheck(skill_ids=["listen"], skill="聆听", on_success="听见"),
                ],
            ),
            ModuleNode(id="street", title="街道", kp_text="…", checks=[]),
        ],
    )


def test_collect_skills() -> None:
    """收集出来的是技能 **id**（exec/17 (A)）。

    这条断言的口径变过两次，都是架构上的合法变化：最早是模组原写法，
    exec/12 #32 起是归一后的规范中文名，现在是 id——模组数据在组装期就
    归一好了，运行时两侧都是 id，中文名不再参与任何比较。
    """
    skills = collect_module_check_skills(_mod())
    assert "spot-hidden" in skills
    assert "侦察" not in skills


def test_filter_blocks_improvised_when_node_has_checks() -> None:
    kept, issues = filter_checks_against_module(
        _mod(),
        ["spot-hidden", "cthulhu-mythos"],
        current_scene="科比特宅邸",
    )
    assert kept == ["spot-hidden"]
    assert any("cthulhu-mythos" in i for i in issues)


def test_filter_allows_when_node_has_no_checks() -> None:
    kept, issues = filter_checks_against_module(
        _mod(),
        ["fast-talk"],
        current_scene="街道",
    )
    assert kept == ["fast-talk"]
    assert issues == []


def test_find_node() -> None:
    m = _mod()
    assert find_node_for_scene(m, "科比特宅邸") is not None
    assert find_node_for_scene(m, "house") is not None


# ── 场景指针结构化：node_id 精确查找优先于自由文本模糊匹配 ──────


def test_find_node_prefers_structured_node_id() -> None:
    m = _mod()
    node = find_node_for_scene(m, "跟剧本地名完全对不上的乱写文本", node_id="house")
    assert node is not None
    assert node.id == "house"


def test_find_node_falls_back_to_scene_hint_when_node_id_missing() -> None:
    m = _mod()
    node = find_node_for_scene(m, "科比特宅邸", node_id=None)
    assert node is not None and node.id == "house"


def test_find_node_falls_back_to_scene_hint_when_node_id_unknown() -> None:
    m = _mod()
    node = find_node_for_scene(m, "科比特宅邸", node_id="no-such-node")
    assert node is not None and node.id == "house"


def test_filter_checks_against_module_uses_node_id() -> None:
    kept, issues = filter_checks_against_module(
        _mod(),
        ["spot-hidden", "cthulhu-mythos"],
        current_scene="跟剧本地名完全对不上的乱写文本",
        current_node_id="house",
    )
    assert kept == ["spot-hidden"]
    assert any("cthulhu-mythos" in i for i in issues)
