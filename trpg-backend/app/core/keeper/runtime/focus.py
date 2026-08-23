"""这一拍要给模型看哪些节点与 NPC 的正文——**关注集**（`exec/47` P1a）。

## 它解决什么

剧本现在是整份注入（`render_full`）。74 节点那份已经吃掉 56,102 / 7 万预算，
只剩 1.25 倍余量，而定位是「长短模组都要支持」。分层注入的形状是
**索引常驻 + 正文按需**，而「按需」的那个需，就是这里算出来的。

## 🔴 为什么由代码算，不交给模型

项目判据：**有便宜可靠的验证器 → agent；没有 → workflow。** 召回错了的表现是
「叙事里少提了一件事」，没有任何东西会变红 ⇒ 归 workflow。

`render_full` 的 docstring 还记着当初放弃「索引 + 让模型自己查」的实测理由：
真实 DeepSeek 连跑三轮，**整轮只调一次工具、NPC 名字现编、检定点视而不见**。
那条路已经证伪过一次，不要再走。

## 五个来源，每一项都已经有 id

1. **当前节点** —— 大部队的 `当前场景节点`，以及分头之后**每个人各自**的位置
2. **它们在合并图上的邻居** —— `merged_graph`，实测平均 2.0–2.6 个
3. **此刻台上的 NPC** —— `cast` 那片能力在记
4. **未结清 `threads` 绑定的地点** —— 「米-戈仍在追击」挂在哪个节点上
5. **上一拍裁决输出里出现过的 id** —— 由调用方传进来（它手上才有那份 decision）

没有一处需要模型判断「相不相关」，也没有一处用到相似度。给定
`keeper_state` 与上一拍裁决，结果**唯一确定** ⇒ 可离线重放、可变异检验、
跑一次不要钱。

## 🔴 孤立节点不在这里解决

合并图上没有任何边的节点（八份模组实测 0–4%）进不了任何关注集。
`exec/47 §5` 原本写的兜底是「靠玩家实际走过的转移补一条运行时边」——
**那条作废了**（2026-08-23 量完之后）：

- 它有鸡生蛋问题。玩家要先**到**那个节点边才建得起来，而模型看不到它的正文
  就不会带玩家去。运行时边只能解决"去过之后还记得"。
- 而且没必要。孤立节点的正文合计 **0–2,043 字符（占 `render_full` 0–3.6%）**，
  直接**常驻**比发明一个新账本便宜得多——不加写入点、不引入上限问题。

`isolated_node_ids()` 就是给注入层用的那份常驻名单。

## 边界

这里只**选 id**。受众裁剪仍在后面（`visible_*` 那一道）：召回错了是少给，
受众错了是泄密，两件事、两道门、各自要有测试。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.core.keeper.capabilities.cast.state import load_on_stage
from app.core.keeper.capabilities.open_threads.state import load_open_threads
from app.core.keeper.contract.module_loader import (
    ScenarioModule,
    iter_all_nodes,
    merged_graph,
)
from app.core.keeper.runtime.location_state import load_player_locations
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY


@dataclass(frozen=True)
class FocusSet:
    """这一拍要给正文的那些 id。

    `reasons` 只为诊断存在——「这个节点为什么进来的」在真机排查时是第一个问题，
    而事后从 `keeper_state` 反推要把整条链重跑一遍。它不进 prompt。
    """

    node_ids: frozenset[str]
    npc_ids: frozenset[str]
    reasons: dict[str, str] = field(default_factory=dict)


def _add(seen: dict[str, str], node_id: str, why: str, valid: set[str]) -> None:
    """记一个 id 与它进来的理由。**第一个理由胜出**——先到的那个更贴近当拍。"""
    if node_id in valid and node_id not in seen:
        seen[node_id] = why


def focus_set(
    module: ScenarioModule,
    keeper_state: dict | None,
    *,
    decision_node_ids: Iterable[str] = (),
    decision_npc_ids: Iterable[str] = (),
) -> FocusSet:
    """算出这一拍的关注集。**纯函数**，同样的输入永远给同样的输出。

    `decision_*` 是上一拍裁决输出里出现过的 id（第 5 项）。调用方传空也完全
    成立——那只是少一个来源，不是坏了。
    """
    state = keeper_state or {}
    valid_nodes = {node.id for node in iter_all_nodes(module.nodes)}
    valid_npcs = {npc.id for npc in module.npcs}
    graph = merged_graph(module)

    seen: dict[str, str] = {}

    # ① 当前节点：大部队 + 分头之后每个人各自的落点。
    #    🔴 两个都要。分头时房间指针一个人都不挪（`multiplayer-split` 那条判据），
    #    只读 `CURRENT_NODE_KEY` 会让分出去的人所在的那一片正文整块拿不到。
    current = str(state.get(CURRENT_NODE_KEY) or "").strip()
    if current:
        _add(seen, current, "当前节点", valid_nodes)
    for node_id in load_player_locations(state).values():
        _add(seen, node_id, "有人在这儿", valid_nodes)

    # ⑤ 上一拍裁决提到的（放在邻居之前算：它比邻居更贴近当拍）
    for node_id in decision_node_ids:
        _add(seen, str(node_id), "上一拍裁决提到", valid_nodes)

    # ④ 未结清的悬而未决绑在哪个节点上
    for thread_id, entry in load_open_threads(state).items():
        node_id = entry.get("node")
        if node_id:
            _add(seen, str(node_id), f"{thread_id} 挂在这儿", valid_nodes)

    # ② 邻居：只从上面那些"人真的在或刚提到"的节点出发扩一层。
    #    🔴 **不迭代扩散**——两层邻居在真实模组里会拉进大半张图（最大度 12），
    #    那就退回整份注入了。
    for node_id in list(seen):
        for peer in graph.get(node_id, ()):
            _add(seen, peer, f"{node_id} 的关联节点", valid_nodes)

    # ③ 台上的 NPC
    npc_ids = {npc_id for npc_id in load_on_stage(state) if npc_id in valid_npcs}
    npc_ids |= {str(npc_id) for npc_id in decision_npc_ids if str(npc_id) in valid_npcs}

    return FocusSet(
        node_ids=frozenset(seen),
        npc_ids=frozenset(npc_ids),
        reasons=dict(seen),
    )


def isolated_node_ids(module: ScenarioModule) -> frozenset[str]:
    """合并图上没有任何边的节点——**它们的正文要常驻**。

    进不了任何关注集，而运行时边解决不了"第一次怎么去"（见模块 docstring）。
    实测八份模组合计 0–2,043 字符，常驻的代价比发明一个转移账本小得多。
    """
    return frozenset(node_id for node_id, peers in merged_graph(module).items() if not peers)
