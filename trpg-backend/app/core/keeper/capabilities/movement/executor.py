"""movement 能力的执行层：场景指针 → 分头移动 → 隐匿，顺序有语义。"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.runtime.deps import KeeperDeps, KeeperToolError
from app.core.keeper.runtime.location_state import (
    clear_current_node_impl,
    create_improvised_location_impl,
    move_player_impl,
    set_current_node_impl,
    set_stealth_impl,
)


def _position_left_the_map(attempted_node_id: str | None, facts: TurnFacts) -> bool:
    """这一轮「人在哪」没能落在任何剧本节点上。

    两种情况语义上是同一件事，都必须走清空：

    - **主路没走**：裁决器换了场景却给不出节点 id（exec/19 #48）。只在它
      **明确写了新的「当前场景」**时才成立——没提场景的普通轮次（对话、检定
      结算）不该动节点指针。
    - **主路失败**：给了 id 但那个 id 不是剧本节点（exec/31 #72，真机三次全中：
      玩家说「去卡比家」，裁决器写了一个 **NPC id**）。它写下这个 id 本身就是在
      说"人已经不在原处了"，所以不必再等场景声明。

    🔴 原来这两支写成 `if / elif`：主路抛异常被 except 吞成 issue 之后，**兜底
    永远轮不到**，指针保留旧值 = 静默说谎（护栏拿错节点的检定表卡玩家、分组也
    跟着错）。判据：**兜底的触发条件要包含「主路失败」，不能只包含「主路没走」。**

    「有没有声明新场景」由 `world_state` publish 进 `TurnFacts`（那个字段是它
    的），本能力只 consume。切分之初这里直接读 `decision.state_updates`——一片
    能力伸手进另一片的字段，没有 import 所以架构测试抓不到，正是最坏的那种
    **隐式**耦合。
    """
    if attempted_node_id:
        return True
    return facts.scene_name_declared is not None


async def execute_movement(
    deps: KeeperDeps, decision: BaseModel, facts: TurnFacts
) -> tuple[list[str], list[str]]:
    """四步顺序不能换：

    0. `new_location` 建表（exec/32）——必须排在最前，这一轮建的地点要能立刻
       被当作落点用；建完**就是发言者的落点**，除非裁决器另写了 `current_node_id`。
    1. `current_node_id` 是"本轮发言者的默认落点"；
    2. `moves` 是"谁不跟大家一起"——必须排在默认落点**之后**，否则被盖掉；
    3. `hiding` 与移动同一类空间状态，逐条执行、逐条记 issue。
    """
    report: list[str] = []
    issues: list[str] = []

    node_id = getattr(decision, "current_node_id", None)
    new_location = getattr(decision, "new_location", None)
    if new_location is not None:
        try:
            created_id, line = await create_improvised_location_impl(
                deps, new_location.name, new_location.from_id
            )
            report.append(line)
            # 建了却没说去哪 = 去的就是这里。裁决器显式写了别的落点时不覆盖它
            # （它可能是"我打发 NPC 去卡比家"这种，人并没有过去）。
            node_id = node_id or created_id
        except KeeperToolError as exc:
            issues.append(f"新地点未建立：{exc}")

    moves = list(getattr(decision, "moves", ()))
    if node_id is not None and any(move.node_id == node_id for move in moves):
        # 🔴 矛盾信号消解（2026-08-10 多人实测）：`moves` 已经把某个人**单独**挪到了
        # `current_node_id` 指的那个地方——那就是"只有他去"的意思。
        #
        # 实测原话「我去地下室看看，阿贵你留在客厅」，裁决器写了
        # `current_node_id=basement-laboratory` **且** `moves=[阿福→basement-laboratory]`
        # （thinking 写着"处理分头"）。而 `current_node_id` 会带上"此刻与发言者同处
        # 的人"（`exec/19 #37` 的默认值），于是被明确留下的阿贵**也被拖进了地下室**：
        # 叙事说他在客厅、结构化位置说他在地下室，两边各说各话（这类只能读事件表
        # 才发现，`exec/19 #43` 同族）。模型随后自己造了 `阿贵位置` 这样的自由文本键
        # 来记它表达不了的东西——「看到模型往奇怪的地方塞，先问它还能塞哪」。
        #
        # 两个字段说的是同一次移动时，**更具体的那个赢**：`moves` 点了名。
        # 这不是给 #37 的默认值加例外，是拒绝执行一条自相矛盾的指令。
        report.append(f"场景定位交给 moves 执行（{node_id} 已被逐人指定）")
        node_id = None

    located = False
    if node_id:
        # node_id 存在性由 set_current_node_impl 校验（module.node_by_id）——
        # 非法 id 不写入、记为 issue，不炸整轮。
        try:
            report.append(await set_current_node_impl(deps, node_id))
            located = True
        except KeeperToolError as exc:
            issues.append(f"场景定位未执行：{exc}")
    if not located and _position_left_the_map(node_id, facts):
        # 🔴 人在剧本节点之外：换了场景却没有节点对应得上（exec/19 #48），
        # 或者给出的 id 根本不是节点（exec/31 #72）。判据见上面那个谓词。
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

    for move in moves:
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
