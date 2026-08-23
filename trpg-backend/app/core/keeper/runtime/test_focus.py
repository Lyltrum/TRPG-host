"""关注集：这一拍要给哪些节点与 NPC 的正文（`exec/47` P1a）。

守两件事：**五个来源一个都不许少**（少一个的表现是"叙事里少提了一件事"，
不会有任何东西变红），以及**它不许悄悄退化成整份注入**（邻居只扩一层）。
"""

from __future__ import annotations

from app.core.keeper.capabilities.cast.state import ON_STAGE_KEY
from app.core.keeper.capabilities.open_threads.state import OPEN_THREADS_KEY
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.runtime.focus import focus_set, isolated_node_ids
from app.core.keeper.runtime.location_state import PLAYER_LOCATION_KEY
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY


def _module(nodes: list[dict], npcs: list[dict] | None = None) -> ScenarioModule:
    return ScenarioModule.model_validate(
        {
            "meta": {"id": "t", "title": "T"},
            "kp_truth": {"summary": "s", "key_facts": []},
            "player_intro": "i",
            "nodes": nodes,
            "npcs": npcs or [],
        }
    )


def _node(node_id: str, **kw) -> dict:
    return {"id": node_id, "title": node_id, "kp_text": f"{node_id} 正文", **kw}


_CHAIN = _module(
    [
        _node("hall", leads_to=["cellar"]),
        _node("cellar", leads_to=["tunnel"]),
        _node("tunnel"),
        _node("attic"),
    ],
    [{"id": "butler", "name": "管家"}, {"id": "maid", "name": "女仆"}],
)


# ═══ 五个来源 ═══


def test_the_current_node_comes_in() -> None:
    focus = focus_set(_CHAIN, {CURRENT_NODE_KEY: "hall"})
    assert "hall" in focus.node_ids
    assert focus.reasons["hall"] == "当前节点"


def test_每个分头的人所在的节点都要进() -> None:
    """🔴 分头时**房间指针一个人都不挪**（`multiplayer-split` 那条判据）。

    只读 `CURRENT_NODE_KEY` 的话，分出去那一半人所在的整片正文拿不到——
    而那正是他们此刻正在经历的地方。**单人局结构上撞不到这条。**
    """
    focus = focus_set(
        _CHAIN,
        {CURRENT_NODE_KEY: "hall", PLAYER_LOCATION_KEY: "p1@attic, p2@hall"},
    )
    assert {"hall", "attic"} <= focus.node_ids


def test_neighbours_on_the_merged_graph_come_in() -> None:
    focus = focus_set(_CHAIN, {CURRENT_NODE_KEY: "hall"})
    assert "cellar" in focus.node_ids
    assert "关联节点" in focus.reasons["cellar"]


def test_npcs_on_stage_come_in() -> None:
    focus = focus_set(_CHAIN, {CURRENT_NODE_KEY: "hall", ON_STAGE_KEY: "butler"})
    assert focus.npc_ids == {"butler"}


def test_the_node_an_open_thread_is_pinned_to_comes_in() -> None:
    """「米-戈仍在追击」挂在哪个节点上，那个节点的正文这一拍就还得在。"""
    focus = focus_set(
        _CHAIN,
        {
            CURRENT_NODE_KEY: "hall",
            OPEN_THREADS_KEY: {"thread-1": {"text": "有人在追", "node": "attic"}},
        },
    )
    assert "attic" in focus.node_ids
    assert "thread-1" in focus.reasons["attic"]


def test_ids_from_the_last_decision_come_in() -> None:
    focus = focus_set(
        _CHAIN,
        {CURRENT_NODE_KEY: "hall"},
        decision_node_ids=["attic"],
        decision_npc_ids=["maid"],
    )
    assert "attic" in focus.node_ids
    assert "maid" in focus.npc_ids


# ═══ 不许退化成整份注入 ═══


def test_neighbours_are_one_hop_only() -> None:
    """🔴 **不迭代扩散。**

    `hall → cellar → tunnel`：`tunnel` 是两跳外的，不该进来。真实模组最大度
    是 12，扩两层就拉进大半张图——那等于绕一圈回到整份注入。
    """
    focus = focus_set(_CHAIN, {CURRENT_NODE_KEY: "hall"})
    assert "cellar" in focus.node_ids
    assert "tunnel" not in focus.node_ids, "两跳外的节点进来了 —— 扩散没收住"


def test_ids_that_are_not_real_nodes_are_dropped() -> None:
    """状态里的 id 是模型写过的，可能指向一个根本不存在的节点。"""
    focus = focus_set(
        _CHAIN,
        {CURRENT_NODE_KEY: "ghost", PLAYER_LOCATION_KEY: "p1@also-ghost"},
        decision_node_ids=["third-ghost"],
        decision_npc_ids=["ghost-npc"],
    )
    assert focus.node_ids == frozenset()
    assert focus.npc_ids == frozenset()


def test_an_empty_state_is_not_an_error() -> None:
    """开局第一拍、以及任何还没落过指针的时刻，都会走到这里。"""
    focus = focus_set(_CHAIN, None)
    assert focus.node_ids == frozenset()
    assert focus.npc_ids == frozenset()


# ═══ 孤立节点 ═══


def test_isolated_nodes_are_listed_for_the_resident_segment() -> None:
    """它们进不了任何关注集 ⇒ 正文只能常驻，否则模型永远看不到。"""
    assert isolated_node_ids(_CHAIN) == {"attic"}
    focus = focus_set(_CHAIN, {CURRENT_NODE_KEY: "hall"})
    assert "attic" not in focus.node_ids, "装置自证：它确实靠关注集是拿不到的"
