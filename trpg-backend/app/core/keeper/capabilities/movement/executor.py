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
from app.core.keeper.registry import TurnFacts


def _scene_moved_off_the_map(decision: BaseModel, facts: TurnFacts) -> bool:
    """本轮换了场景，却没给出任何剧本节点 id（exec/19 #48）。

    只在裁决器**明确写了新的「当前场景」**时才成立——没提场景的普通轮次
    （对话、检定结算）不该动节点指针。

    「有没有声明新场景」由 `world_state` publish 进 `TurnFacts`（那个字段是它
    的），本能力只 consume。切分之初这里直接读 `decision.state_updates`——一片
    能力伸手进另一片的字段，没有 import 所以架构测试抓不到，正是最坏的那种
    **隐式**耦合。
    """
    if getattr(decision, "current_node_id", None):
        return False
    return facts.scene_name_declared is not None


async def execute_movement(
    deps: KeeperDeps, decision: BaseModel, facts: TurnFacts
) -> tuple[list[str], list[str]]:
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
    elif _scene_moved_off_the_map(decision, facts):
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
