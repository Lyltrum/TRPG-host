"""closure 能力的执行层：记「去过哪」+ 反向门 + 自然收尾。

## 反向门：只禁危险方向

收尾判错的代价**不对称**：
- 「该收没收」→ 下一轮再收，玩家多聊两句，**可恢复**；
- 「不该收却收了」→ 对局落幕、行动被拒，**不可撤回**（同「保密靠拿不到」一族）。

所以这里不去判断"故事是不是真的完了"（那是语义，代码做不了），只在**明显
还没完**的时候拦住：还有一次性议程没触发、或者本轮刚揭开新线索。判断留给
裁决器，代码只守住不可逆的那一侧。

🔴 这也是它**不问人**的理由：`exec/30 §10.3` 里「agent 提议 → 房主确认」被
否掉了——房主也是玩家，他对剧本同样未知，那张卡片问的是「故事到这儿了吗」，
**需要读过剧本才答得了，而全桌按设计就没人读过**。不能把判断交给唯一没有
信息的一方。
"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.keeper.capabilities.closure.remaining import unfired_agenda_count
from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.runtime.deps import KeeperDeps, KeeperToolError, record_event
from app.core.keeper.runtime.location_state import load_player_locations
from app.core.keeper.runtime.phase import PHASE_FINISHED, load_phase, set_phase_impl
from app.core.keeper.runtime.progress_state import (
    VISITED_NODES_KEY,
    load_visited_nodes,
    serialize_visited_nodes,
)
from app.models.room import Room


async def _record_visits(deps: KeeperDeps) -> list[str]:
    """把此刻所有人所在的节点并进「去过的节点」。返回本轮新去的那些。

    🔴 **回合级**，不挂在每个写位置的函数上：位置有三条写入路径
    （`current_node_id` / `moves` / 即兴地点），逐个挂等于"逐个列出的地方，
    加一项就漏一项"。读回合结束后的位置表则天然覆盖全部路径，且幂等。
    """
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        visited = load_visited_nodes(current_state)
        here = [nid for nid in load_player_locations(current_state).values() if nid]
        newly = [nid for nid in dict.fromkeys(here) if nid not in visited]
        if not newly:
            return []
        visited.extend(newly)
        current_state[VISITED_NODES_KEY] = serialize_visited_nodes(visited)
        room.keeper_state = current_state
        await record_event(db, deps, "keeper.visited", {"node_ids": newly})
    return newly


async def execute_closure(
    deps: KeeperDeps, decision: BaseModel, facts: TurnFacts
) -> tuple[list[str], list[str]]:
    report: list[str] = []
    issues: list[str] = []

    try:
        newly = await _record_visits(deps)
    except KeeperToolError as exc:
        issues.append(f"到过的地方未记账：{exc}")
        newly = []
    if newly:
        report.append("去过的地方新增：" + "、".join(newly))

    if not getattr(decision, "story_ran_its_course", False):
        return report, issues

    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        keeper_state = dict(room.keeper_state or {}) if room is not None else {}

    # progression（order=80）先跑：本轮已经按剧本结局收束过了就不重复收。
    if load_phase(keeper_state) == PHASE_FINISHED:
        return report, issues

    if facts.clues_revealed_this_turn:
        issues.append("自然收尾未执行：本轮刚揭开新线索，故事还在往下走")
        return report, issues

    unfired = unfired_agenda_count(deps.module, keeper_state)
    if unfired:
        issues.append(f"自然收尾未执行：还有 {unfired} 条一次性议程没发生")
        return report, issues

    try:
        # 不带 ending_id：这一局没有命中任何**预设**结局，它是跑完了。
        # 记成 ending_id 会凭空造一个剧本里不存在的结局 id。
        report.append(await set_phase_impl(deps, PHASE_FINISHED))
        report.append("故事自然收尾（内容已跑完，无预设结局命中）")
    except KeeperToolError as exc:
        issues.append(f"自然收尾未执行：{exc}")
    return report, issues
