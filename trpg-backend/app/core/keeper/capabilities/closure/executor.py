"""closure 能力的执行层：记「去过哪」+ 反向门 + 自然收尾。

## 🔴 2026-08-12：收尾落在 `ending`，不再直接 `finished`

原先一判就 `finished`，那是**一堵硬墙**（`agent.py` 直接返回"本局已结束"，
模型都不再跑）。于是"收早了"变得极贵，逼得规则 10b 给 KP 加了一道
「三个数都见底才准收」的机械前提——**代码替 KP 做了它本来就该做的判断**，
而真人 KP 从来不受这个限制。真人反馈实证的正是它的另一头：玩家一直偏离
主线，三个数永远不见底，于是**永远等不到落幕**。

修法不是把阈值调准，是**让判错的代价变小**：落在 `ending`（叙事写终章，
行动照常受理），玩家接着说话就自动退回 `investigation`（见 `agent.py`）。
边界画不准就不必画准了。

> **能确定化的是判断的输入，不是判断本身。**

## 反向门：只禁自相矛盾的那一条

代价变小之后，门也跟着收窄到只剩**自打嘴巴**的那一种：本轮刚揭开新线索还要
同时收尾。「还有一次性议程没触发」不再拦——真人 KP 完全可以在议程没跑完时
收尾，那条同样是替他做决定，现在降级成局面块里的一个数。

🔴 这也是它**不问人**的理由：`exec/30 §10.3` 里「agent 提议 → 房主确认」被
否掉了——房主也是玩家，他对剧本同样未知，那张卡片问的是「故事到这儿了吗」，
**需要读过剧本才答得了，而全桌按设计就没人读过**。不能把判断交给唯一没有
信息的一方。
"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.runtime.deps import KeeperDeps, KeeperToolError, record_event
from app.core.keeper.runtime.location_state import load_player_locations
from app.core.keeper.runtime.phase import (
    PHASE_ENDING,
    PHASE_FINISHED,
    load_phase,
    set_phase_impl,
)
from app.core.keeper.runtime.progress_state import (
    STALLED_TURNS_KEY,
    VISITED_NODES_KEY,
    load_stalled_turns,
    load_visited_nodes,
    serialize_visited_nodes,
)
from app.models.room import Room


async def _record_progress(deps: KeeperDeps, *, clues_revealed: bool) -> list[str]:
    """记「去过的节点」，并顺手维护「无进展轮数」。返回本轮新去的那些。

    🔴 **回合级**，不挂在每个写位置的函数上：位置有三条写入路径
    （`current_node_id` / `moves` / 即兴地点），逐个挂等于"逐个列出的地方，
    加一项就漏一项"。读回合结束后的位置表则天然覆盖全部路径，且幂等。

    两件事写在同一个锁里，是因为它们读的是同一份 `keeper_state`：分成两次
    读-改-写，后一次会拿着旧快照覆盖前一次刚写进去的键。
    """
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        visited = load_visited_nodes(current_state)
        here = [nid for nid in load_player_locations(current_state).values() if nid]
        newly = [nid for nid in dict.fromkeys(here) if nid not in visited]
        if newly:
            visited.extend(newly)
            current_state[VISITED_NODES_KEY] = serialize_visited_nodes(visited)

        # 进展的口径只有这两样，因为只有它们是**代码确定性可判**的：去了新地方、
        # 揭开了新线索。"剧情有没有推进"是语义，不进这个计数。
        if newly or clues_revealed:
            current_state[STALLED_TURNS_KEY] = 0
        else:
            current_state[STALLED_TURNS_KEY] = load_stalled_turns(current_state) + 1

        room.keeper_state = current_state
        if newly:
            await record_event(db, deps, "keeper.visited", {"node_ids": newly})
        else:
            # 🔴 `record_event` 里那次 commit 此前是**唯一**的提交点，于是"没去新
            # 地方"这一支直接返回、什么都不落库。加了停滞计数之后那一支也有东西
            # 要写了——恰恰是最需要写的那一支（原地打转的每一轮）。
            await db.commit()
    return newly


async def execute_closure(
    deps: KeeperDeps, decision: BaseModel, facts: TurnFacts
) -> tuple[list[str], list[str]]:
    report: list[str] = []
    issues: list[str] = []

    try:
        newly = await _record_progress(deps, clues_revealed=bool(facts.clues_revealed_this_turn))
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
    phase = load_phase(keeper_state)
    if phase in (PHASE_FINISHED, PHASE_ENDING):
        return report, issues

    if facts.clues_revealed_this_turn:
        issues.append("自然收尾未执行：本轮刚揭开新线索，故事还在往下走")
        return report, issues

    try:
        # 不带 ending_id：这一局没有命中任何**预设**结局，它是跑完了。
        # 记成 ending_id 会凭空造一个剧本里不存在的结局 id。
        report.append(await set_phase_impl(deps, PHASE_ENDING))
        report.append("故事进入收尾（无预设结局命中；玩家继续行动会退回调查阶段）")
    except KeeperToolError as exc:
        issues.append(f"自然收尾未执行：{exc}")
    return report, issues
