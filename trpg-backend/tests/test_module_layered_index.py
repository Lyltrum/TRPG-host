"""分层注入的两块地基：合并图与索引渲染（`exec/47` P0）。

## 这一层守的是什么

P0 的产物**没有消费方**——它不改注入，只是把 `merged_graph` 与 `render_index`
建出来并量准，用来**证伪**整个分层方案压着的那个前提：合并图够不够密。
前提不成立，P1 就不该动手。

所以这里有两类测试，性质不同：

- **单元**：图的形状（无向 / 丢悬空 / 父子无条件成边）与索引的红线（不许有正文）。
- **全量**：八份真模组一起跑，孤立率与索引占比不许越线。它跟
  `test_import_regression_corpus` 同一个做法——`模组资料/` 是 gitignored 的，
  CI 上没有 ⇒ 缺数据就 skip。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.core.keeper.contract.module_loader import (
    ScenarioModule,
    iter_all_nodes,
    merged_graph,
    render_index,
    render_node,
    render_node_index,
    render_npc_index,
)
from app.core.keeper.runtime.focus import focus_set, isolated_node_ids, should_layer
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY

_BACKEND = Path(__file__).resolve().parents[1]
_CORPUS = _BACKEND.parent / "模组资料"
_DEV_DB = _BACKEND / "app.db"

#: 🔴 **孤立率的线定在 15%**，而八份真模组实测 0–4%（2026-08-23）。
#:
#: 它不是「调出来刚好通过」的阈值——留这么宽的余量是因为它的作用是**报警**：
#: 孤立节点进不了任何关注集，模型永远看不到它们的正文。真越了线，该做的是回
#: `exec/47` 重新设计召回，不是把这个数字调大。
_ISOLATED_CEILING = 0.15


def _module(**kw) -> ScenarioModule:
    base = {
        "meta": {"id": "t", "title": "T"},
        "kp_truth": {"summary": "s", "key_facts": []},
        "player_intro": "i",
    }
    return ScenarioModule.model_validate({**base, **kw})


def _node(node_id: str, **kw) -> dict:
    return {"id": node_id, "title": node_id, "kp_text": f"{node_id} 的绝密正文", **kw}


# ═══ 图的形状 ═══


def test_the_graph_is_undirected() -> None:
    """`a.leads_to = [b]` 时，站在 b 上也要能召回 a。"""
    module = _module(nodes=[_node("a", leads_to=["b"]), _node("b")])
    graph = merged_graph(module)
    assert graph["a"] == {"b"}
    assert graph["b"] == {"a"}, "单向的话，站在 b 上就看不到是什么把玩家引来的"


def test_the_three_edge_kinds_are_merged() -> None:
    module = _module(
        nodes=[
            _node("a", exits=["b"], leads_to=["c"], contains=["d"]),
            _node("b"),
            _node("c"),
            _node("d"),
        ]
    )
    assert merged_graph(module)["a"] == {"b", "c", "d"}


def test_dangling_ids_are_dropped() -> None:
    """模组数据里的边可能指向不存在的节点（LLM 抽出来的）。"""
    module = _module(nodes=[_node("a", leads_to=["ghost"], exits=["a"])])
    graph = merged_graph(module)
    assert graph == {"a": set()}, "悬空 id 与自环都不该进图"


def test_nesting_makes_an_edge_even_without_contains() -> None:
    """🔴 父子嵌套是结构性事实，不许依赖 LLM 有没有写 `contains`。

    八份模组里追书人的 `speakeasy` 正是这么漏掉的：它是子节点，父节点没写
    `contains` 指向它 ⇒ 只看字段的图里它孤立。
    """
    module = _module(
        nodes=[
            {
                **_node("parent"),
                "sub_nodes": [_node("child")],
                "sub_node": _node("legacy-child"),
            }
        ]
    )
    graph = merged_graph(module)
    assert graph["parent"] == {"child", "legacy-child"}
    assert graph["child"] == {"parent"}
    assert graph["legacy-child"] == {"parent"}


# ═══ 索引的红线 ═══


def test_the_index_carries_no_prose_at_all() -> None:
    """🔴 索引常驻 ⇒ 进了索引就等于提前发出去了。

    `kp_text` 不必说；`public_text` 也不给——它是「挣得/公开之后才念给玩家」的
    东西（「保密靠拿不到」）。断言选的是**整段正文**而不是某个词，反例装不下。
    """
    module = _module(
        nodes=[
            {
                **_node("hall"),
                "kp_text": "凶手把钥匙藏在地毯下",
                "public_text": "门厅铺着长地毯",
            }
        ],
        npcs=[
            {
                "id": "butler",
                "name": "管家",
                "role": "仆人",
                "kp_notes": "他就是凶手",
                "public_text": "他今晚一直在擦银器",
            }
        ],
    )
    text = render_index(module)
    for leak in ("凶手把钥匙藏在地毯下", "门厅铺着长地毯", "他就是凶手", "他今晚一直在擦银器"):
        assert leak not in text, f"索引泄露了正文：{leak}"
    # 该有的仍要有：id 与显示名，模型靠它们不现编专有名词
    assert "hall" in text and "管家" in text and "butler" in text
    assert "仆人" in text, "公开身份是标题级别，不给的话模型认不出这个 id 是谁"


def test_the_index_covers_every_node_including_children() -> None:
    """漏掉的节点等于从世界上消失——模型连它的 id 都不知道。"""
    module = _module(nodes=[{**_node("parent"), "sub_nodes": [_node("child")]}, _node("lonely")])
    index = render_node_index(module)
    for node in iter_all_nodes(module.nodes):
        assert node.id in index, f"{node.id} 不在索引里"


def test_the_npc_index_covers_every_npc() -> None:
    module = _module(npcs=[{"id": "a", "name": "甲"}, {"id": "b", "name": "乙", "role": "船长"}])
    index = render_npc_index(module)
    assert "a" in index and "甲" in index
    assert "b" in index and "乙" in index and "船长" in index


# ═══ 八份真模组全量 ═══


def _corpus() -> list[tuple[str, ScenarioModule]]:
    out: list[tuple[str, ScenarioModule]] = []
    for name in ("追书人", "科比特先生", "神秘渡轮", "复足", "死者的顿足舞"):
        path = _CORPUS / f"{name}.structured.json"
        if path.exists():
            out.append((name, ScenarioModule.model_validate(json.loads(path.read_text("utf-8")))))
    if _DEV_DB.exists():
        conn = sqlite3.connect(str(_DEV_DB))
        try:
            rows = list(conn.execute("select structured from imported_modules"))
        except sqlite3.DatabaseError:  # pragma: no cover - 库结构对不上就当没有
            rows = []
        finally:
            conn.close()
        for (raw,) in rows:
            data = json.loads(raw)
            out.append((f"[导入] {data['meta']['id']}", ScenarioModule.model_validate(data)))
    return out


@pytest.mark.skipif(not _CORPUS.exists(), reason="第三方模组是 gitignored 的，CI 上没有")
def test_every_real_module_has_a_dense_enough_graph() -> None:
    """整个分层方案压在这个前提上：合并图够密。**它不成立就别写注入。**

    🔴 **只对真正会走分层的模组断言**（2026-08-25 收窄）。退化路径上的模组
    整份注入，**根本没有"关注集"这回事**——原来那句失败信息（"它们进不了任何
    关注集"）对它们本身就不成立。一份 9,508 字符的模组孤立率 18%，实际危害是
    **零**，因为那 4 个节点照样一字不落地进 prompt。

    全量的孤立率仍然**打印出来**当探针：导入管线确实能产出 18% 这种模组，
    今天无害只因为那一份小；哪天一份大模组也这样，这条断言就会拦住它。
    **能确定的那一半拦，判不准的只记不断**——同 `_entity_name_in_key` 的先例。
    """
    corpus = _corpus()
    assert len(corpus) >= 5, f"样本太少（{len(corpus)} 份），量出来的比例不作数"

    for label, module in corpus:
        graph = merged_graph(module)
        isolated = [node_id for node_id, peers in graph.items() if not peers]
        ratio = len(isolated) / len(graph)
        layered = should_layer(module)
        print(
            f"[孤立率] {label}: {ratio:.0%}（{len(isolated)}/{len(graph)}）"
            f" {'· 走分层' if layered else '· 退化路径，本条不拦'}"
        )
        if not layered:
            continue
        assert ratio <= _ISOLATED_CEILING, (
            f"{label}：{len(isolated)}/{len(graph)} 个节点在合并图上孤立"
            f"（{ratio:.0%}），它们进不了任何关注集 ⇒ 回 exec/47 重新设计召回"
        )


@pytest.mark.skipif(not _CORPUS.exists(), reason="第三方模组是 gitignored 的，CI 上没有")
def test_no_real_module_leaks_kp_text_into_its_index() -> None:
    """红线的全量版：真模组的 `kp_text` 一个字都不许出现在索引里。"""
    for label, module in _corpus():
        index = render_index(module)
        for node in iter_all_nodes(module.nodes):
            body = (node.kp_text or "").strip()
            if len(body) < 12:  # 太短的正文可能整段就是个标题，不构成泄露判据
                continue
            assert body not in index, f"{label} 的 {node.id} 把 kp_text 漏进了索引"


# ═══ 关注集：八份真模组，每个节点各当一次起点（exec/47 P1a）═══

#: 🔴 **一次召回最多能拉进多大一片正文**（字符）。
#:
#: 八份模组每个节点各当一次起点，**实测最大 7,921**（死者的顿足舞的
#: `funeral-scene`，13 个节点——正是那份模组里度数最高的那个）。其余七份的
#: 最大值都在 4,386 以下，顿足舞是离群点：它只有 10 个顶层节点却挂了 21 个
#: 子节点，节点中位长度是别人的两到三倍。
#:
#: 🔴 **它抓不到"邻居扩散变两跳"，别指望它守这个。** 实测扩到两跳只涨
#: **1.3–2.1 倍**（八份最大 11,087，仍在这条线以下）——我原本在这里写的
#: 「两跳会翻几倍、顿足舞上是几万」是**推理出来的，量完是错的**。守一跳的
#: 是 `test_neighbours_are_one_hop_only`（单元，扩两跳当场红）。
#:
#: 这条线抓的是**量级失控**：哪个来源开始无脑全塞、或者过滤失效把整张图拉进来。
#: 真越线时该看的是"谁把它撑大了"，不是把这个数调大。
#:
#: （第一版按 p90 定成 5,500，当场被顿足舞打红——**p90 不是最大值**。）
_RECALL_CEILING = 12000


@pytest.mark.skipif(not _CORPUS.exists(), reason="第三方模组是 gitignored 的，CI 上没有")
def test_no_single_beat_recalls_a_huge_slab_of_the_script() -> None:
    """召回段不许悄悄长回整份注入的量级。

    分层的全部意义在于这个数**不随模组变大**——真正随节点数涨的只有索引。
    """
    for label, module in _corpus():
        by_id = {node.id: node for node in iter_all_nodes(module.nodes)}
        for node_id in by_id:
            focus = focus_set(module, {CURRENT_NODE_KEY: node_id})
            chars = sum(len(render_node(by_id[k])) for k in focus.node_ids)
            assert chars <= _RECALL_CEILING, (
                f"{label} 站在 {node_id} 上时召回了 {chars} 字符"
                f"（{len(focus.node_ids)} 个节点）——检查扩散是不是变成了两跳"
            )


@pytest.mark.skipif(not _CORPUS.exists(), reason="第三方模组是 gitignored 的，CI 上没有")
def test_standing_on_a_node_always_recalls_that_node() -> None:
    """最起码的一条：**人在哪儿，那儿的正文就得在**。

    看着是废话，但它是整条链上唯一"漏了会毁掉这一拍"的来源——其余四个来源
    漏掉只是少给一点上下文。
    """
    for label, module in _corpus():
        for node in iter_all_nodes(module.nodes):
            focus = focus_set(module, {CURRENT_NODE_KEY: node.id})
            assert node.id in focus.node_ids, f"{label} 的 {node.id} 站上去都拿不到自己"


@pytest.mark.skipif(not _CORPUS.exists(), reason="第三方模组是 gitignored 的，CI 上没有")
def test_the_resident_slab_of_isolated_nodes_stays_small() -> None:
    """孤立节点走常驻——**前提是它们真的很小**。

    实测 0–2,043 字符（占 `render_full` 0–3.6%）。这条线一旦被越过，"直接常驻"
    这个决定本身就要重新算账（那时才轮到运行时边或者别的办法）。
    """
    for label, module in _corpus():
        # 🔴 同上条：**常驻孤立节点这件事只发生在分层路径上**。退化路径整份注入，
        # 没有"常驻块"也就无所谓划不划算。
        if not should_layer(module):
            continue
        by_id = {node.id: node for node in iter_all_nodes(module.nodes)}
        chars = sum(len(render_node(by_id[k])) for k in isolated_node_ids(module))
        ratio = chars / len(render_index(module))
        assert chars <= 4000, f"{label} 的孤立节点正文有 {chars} 字符，常驻不划算了"
        assert ratio < 1.0, f"{label} 的孤立节点正文比整份索引还大（{chars}）"
