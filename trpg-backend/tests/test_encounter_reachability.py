"""遭遇有了自己的归宿，而且必须走得到（`exec/30 §9`）。

## 症状

真机那份林中屋产物**第三幕整幕不是节点**：`endings`=0，那一幕（伪装 / 揭穿 /
武器 / 撤退 / 战后）一个 node 都没有，材料被压成一段摘要塞进
`npcs[].kp_notes`，而 **13 道门全绿**。同一条管线同一份原文，上一次导入那一幕
是 5 个独立顶层 node，接成一条完整的 leads_to 链。

## 根因：第三次「塞进最像的那个槽」

前两次是 pregen 挤进 `player_intro`、目录挤进 `meta`。这次是**遭遇 / 对抗没有
归宿**——它不是地点，也不是人，而 `npcs[].kp_notes` 是个无限大的自由文本袋，
整幕塞进去完全合法。修法同族：**给它一个定义得很窄的归宿**（node 的一种，
`kind="encounter"`——它必须进 Transition 才可达），并收窄 `kp_notes` 的定义。

## 🔴 这道门守的是修法的后一半，不是那次失败本身

坏产物里**一个 encounter 节点都没有**，所以这道门抓不到它。它防的是修法半途
而废：遭遇成了节点、却悬在图外——那一幕照样永远不会发生。

「材料该不该成为节点」是语义判断，**没有机械判据**。定这道门时逐个量废了七个
候选，全都在内置模组上误伤或在真实坏样本上失灵，记录见 `exec/30 §9`。
"""

from __future__ import annotations

import pytest

from app.core.keeper.contract.module_loader import ScenarioModule
from scripts.module_probe.validate_module import check_encounter_reachability


def _module(nodes: list[dict]) -> ScenarioModule:
    return ScenarioModule.model_validate(
        {
            "meta": {"id": "m", "title": "T"},
            "kp_truth": {"summary": "s", "key_facts": []},
            "player_intro": "intro",
            "nodes": nodes,
        }
    )


def _node(nid: str, **kw) -> dict:
    return {"id": nid, "title": nid, "kp_text": "x", **kw}


# ── 两头标定：必然通过那头 ──────────────────────────────


def test_a_wired_encounter_chain_passes() -> None:
    """必然通过那头取自真实产物的图形状。

    上一次导入把那一幕接成了一条链：客厅 → 伪装 → 揭穿 → {武器, 撤退} → 战后，
    **五个节点全都有入边**。照它的形状建模，这道门必须一条都不报。
    """
    module = _module(
        [
            _node("living-room", leads_to=["mi-go-disguise"]),
            _node("mi-go-disguise", kind="encounter", leads_to=["unmasking-mi-go"]),
            _node(
                "unmasking-mi-go",
                kind="encounter",
                leads_to=["darkness-weapon", "mi-go-retreat"],
            ),
            _node("darkness-weapon", kind="encounter"),
            _node("mi-go-retreat", kind="encounter", leads_to=["after-battle"]),
            _node("after-battle", kind="encounter"),
        ]
    )

    assert check_encounter_reachability(module) == []


@pytest.mark.parametrize("edge", ["exits", "contains"])
def test_any_kind_of_incoming_edge_counts(edge: str) -> None:
    """三种边都算「接上了」——空间邻接与包含层级同样能把玩家带到那一幕。"""
    module = _module(
        [
            _node("hall", **{edge: ["ambush"]}),
            _node("ambush", kind="encounter"),
        ]
    )

    assert check_encounter_reachability(module) == []


def test_sub_node_encounter_is_reachable_through_its_parent() -> None:
    """挂在父节点下的遭遇由父节点带上场，不该被报成悬空。"""
    module = _module(
        [_node("cellar", sub_nodes=[_node("rat-swarm", kind="encounter")])],
    )

    assert check_encounter_reachability(module) == []


# ── 两头标定：必然失败那头 ──────────────────────────────


def test_removing_the_one_edge_that_starts_the_act_fails() -> None:
    """必然失败那头是上面那条链的**一条边**之差。

    把客厅指向伪装者的那条 leads_to 拿掉——整幕就从图上脱落了。刻意只改一条边：
    造得跟真货一样难分，否则标定只是在夸自己。
    """
    module = _module(
        [
            _node("living-room"),  # ← 唯一的差别：leads_to 没了
            _node("mi-go-disguise", kind="encounter", leads_to=["unmasking-mi-go"]),
            _node("unmasking-mi-go", kind="encounter"),
        ]
    )

    errors = check_encounter_reachability(module)

    assert len(errors) == 1
    assert "mi-go-disguise" in errors[0]


def test_the_error_says_how_to_fix_it() -> None:
    """错误话术要给出修法——自修器和人都靠这句话决定改什么。

    最怕的修法是「把 kind 改掉」或「删掉这个节点」：门会绿，那一幕照样没了。
    """
    (error,) = check_encounter_reachability(
        _module([_node("ambush", kind="encounter")]),
    )

    assert "leads_to" in error and "exits" in error


# ── 🔴 与已被毙掉的「孤立节点」门划清界限 ─────────────────


def test_a_lone_background_node_is_not_an_error() -> None:
    """泛化的「孤立节点」门 §8.5 试过，**内置 6 份模组里 4 份命中**——背景资料
    条目本来就不是"走得到的地方"。实测同一份好产物 23 个顶层节点里 11 个没有
    入边，全部合法。

    所以这道门只认 `kind="encounter"`：那是组装层**自己声明**的封闭类别，
    语义就是「玩家会撞上的一幕」。这条用例是这道门不退化成那个假门的守卫。
    """
    module = _module(
        [
            _node("background-info"),
            _node("investigator-info"),
            _node("library-info", kind="clue"),
        ]
    )

    assert check_encounter_reachability(module) == []


def test_kind_matching_ignores_case_and_padding() -> None:
    """kind 是模型写的自由文本，大小写/空格不该让门静默失效。"""
    module = _module([_node("ambush", kind="  Encounter ")])

    assert check_encounter_reachability(module) != []
