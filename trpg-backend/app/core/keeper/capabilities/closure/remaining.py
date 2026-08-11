"""「这一局还剩多少内容」的三个计数，每轮摆到裁决器眼前。

## 为什么是局面块，不是门

同 `format_endings_status` 的形状：**把该判断的东西摆到眼前，比在规则里多写
一句可靠**。这三个数回答的是「这一局走到哪了」——对着模组自己定义的格子数，
**不给模组的结构打分**。（那七个被量废的候选问的是另一件事：「这份模组结构
好不好」，那是对内容下判断，做不到。）

## 🔴 缺数据要显式降级，不许报 0

导入进来的模组曾经**根本不产 `facts` / `visibility_pairs`**，于是「未揭开配对」
对它们恒为 0——而 0 跟"全都揭开了"长得一模一样，正好把收尾门推向放行。
**缺数据必须说出来**（"这份模组没有配对数据"），这是明令禁止的静默兜底那一族。
"""

from __future__ import annotations

from app.core.keeper.contract.module_loader import ScenarioModule, iter_all_nodes
from app.core.keeper.contract.registry import SituationContext
from app.core.keeper.runtime.progress_state import (
    is_pair_revealed,
    load_fired_agenda,
    load_revealed_clues,
    load_visited_nodes,
)


def unrevealed_pair_count(module: ScenarioModule, keeper_state: dict | None) -> int | None:
    """还有多少条线索配对没揭开。**没有配对数据时返回 None**，不是 0。"""
    if not module.visibility_pairs:
        return None
    revealed = load_revealed_clues(keeper_state)
    return sum(1 for pair in module.visibility_pairs if not is_pair_revealed(revealed, pair.id))


def unfired_agenda_count(module: ScenarioModule, keeper_state: dict | None) -> int | None:
    """还有多少条 `once` 议程没触发。没有议程表时返回 None。

    只数 `once=True` 的：`once=False` 的事件可以反复发生，"还没发生过"对它
    不成立，拿它当"内容没跑完"的证据会让对局永远收不了尾。
    """
    once_events = [e for e in module.agenda if e.once]
    if not once_events:
        return None
    fired = set(load_fired_agenda(keeper_state))
    return sum(1 for event in once_events if event.id not in fired)


def unvisited_node_count(module: ScenarioModule, keeper_state: dict | None) -> int | None:
    """还有多少个剧本节点没去过。没有节点的模组返回 None。

    🔴 口径是**扁平遍历**（`iter_all_nodes`）：子节点有 `sub_node`（单数）与
    `sub_nodes`（复数）**两个**字段，少走一个就少数一批。只数顶层更糟——
    "林中屋只有 4 个 node"那个假数字就是这么来的，我据此下过大结论。
    """
    all_nodes = iter_all_nodes(module.nodes)
    if not all_nodes:
        return None
    visited = set(load_visited_nodes(keeper_state))
    return sum(1 for node in all_nodes if node.id not in visited)


def _line(label: str, remaining: int | None, total: int, missing_note: str) -> str:
    if remaining is None:
        return f"- {label}：{missing_note}"
    return f"- {label}：还剩 {remaining} / 共 {total}"


def format_remaining_content(module: ScenarioModule, keeper_state: dict | None) -> str:
    """渲染三行计数。三样都缺就整块省略（没有任何依据可摆）。"""
    pairs = unrevealed_pair_count(module, keeper_state)
    agenda = unfired_agenda_count(module, keeper_state)
    nodes = unvisited_node_count(module, keeper_state)
    if pairs is None and agenda is None and nodes is None:
        return ""
    return "\n".join(
        [
            _line(
                "未揭开的线索配对",
                pairs,
                len(module.visibility_pairs),
                "这份模组没有配对数据，据此判断不了还剩多少线索",
            ),
            _line(
                "未触发的议程（只数一次性的）",
                agenda,
                len([e for e in module.agenda if e.once]),
                "这份模组没有一次性议程",
            ),
            _line(
                "没去过的地方",
                nodes,
                len(iter_all_nodes(module.nodes)),
                "这份模组没有节点",
            ),
        ]
    )


def render_remaining_content(context: SituationContext) -> str:
    return format_remaining_content(context.module, context.keeper_state)
