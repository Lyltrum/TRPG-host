"""movement 能力的执行层：场景指针 → 分头移动 → 隐匿，顺序有语义。"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.keeper.deps import KeeperDeps, KeeperToolError
from app.core.keeper.location_state import (
    clear_current_node_impl,
    move_player_impl,
    set_current_node_impl,
    set_stealth_impl,
)

#: `state_updates` 里那个人类可读的场景键。它和 `current_node_id` 是同一件事的
#: 两个面（人读的地名 / 机器读的节点 id），两者脱节就会出 exec/19 #48。
_SCENE_KEY = "当前场景"


def _scene_moved_off_the_map(decision: BaseModel) -> bool:
    """本轮换了场景，却没给出任何剧本节点 id（exec/19 #48）。

    只在裁决器**明确写了新的「当前场景」**时才成立——没提场景的普通轮次
    （对话、检定结算）不该动节点指针。

    ⚠️ 如实记一处**跨能力耦合**（exec/27 阶段 3）：这里读的
    `state_updates` 是 `world_state` 的字段。「当前场景」是人类可读地名、
    走自由文本记账，而节点指针是本能力的——这条规则天然横跨两片。没有 import
    跨过去（架构测试因此不会报），但耦合是真的。**根治要把「当前场景」提升成
    本能力的一等字段**，那是 schema 变更＋行为变更，不在阶段 3 范围内。
    """
    if getattr(decision, "current_node_id", None):
        return False
    return any(
        u.key == _SCENE_KEY and u.value.strip() for u in getattr(decision, "state_updates", ())
    )


async def execute_movement(deps: KeeperDeps, decision: BaseModel) -> tuple[list[str], list[str]]:
    """三步顺序不能换：

    1. `current_node_id` 是"本轮发言者的默认落点"；
    2. `moves` 是"谁不跟大家一起"——必须排在默认落点**之后**，否则被盖掉；
    3. `hiding` 与移动同一类空间状态，逐条执行、逐条记 issue。
    """
    report: list[str] = []
    issues: list[str] = []

    node_id = getattr(decision, "current_node_id", None)
    if node_id:
        # node_id 存在性由 set_current_node_impl 校验（module.node_by_id）——
        # 非法 id 不写入、记为 issue，不炸整轮。
        try:
            report.append(await set_current_node_impl(deps, node_id))
        except KeeperToolError as exc:
            issues.append(f"场景定位未执行：{exc}")
    elif _scene_moved_off_the_map(decision):
        # 🔴 场景变了、但没有任何剧本节点对应得上（exec/19 #48）。
        #
        # 试玩实测：终局「当前场景 = 科比特家门外（警察到场）」，而节点指针还
        # 停在 basement-laboratory——玩家已经站在屋外，护栏却拿地下室的 checks[]
        # 去卡他的检定。裁决器**做对了**（找不到对应节点就留空，不编造 id），
        # 错在代码把"没说"当成了"没变"。
        #
        # 正确语义是**清空**：人在剧本节点之外的地方，护栏退化到即兴层放行。
        # 与 #37 同族——空间状态是地基，宁可承认不知道，不可拿旧值硬撑。
        try:
            cleared = await clear_current_node_impl(deps)
            if cleared:
                report.append(cleared)
        except KeeperToolError as exc:
            issues.append(f"场景指针清空未执行：{exc}")

    for move in getattr(decision, "moves", ()):
        try:
            report.append(await move_player_impl(deps, move.player, move.node_id))
        except KeeperToolError as exc:
            issues.append(f"分头移动未执行：{exc}")

    for change in getattr(decision, "hiding", ()):
        try:
            report.append(await set_stealth_impl(deps, change.player, change.hidden))
        except KeeperToolError as exc:
            issues.append(f"潜行状态未执行：{exc}")

    return report, issues
