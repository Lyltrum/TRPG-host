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

## 🔴 2026-08-13 翻案：门回来了，改的是它数什么

上午我把这道门整个拆了，理由是"边界画不准就让判错的代价变小"。**那是绕过
bug，不是修 bug**：门本身没错，错的是它数错了东西——「没去过的地方」的分母
是扁平展开的全部节点，而玩家位置只落在地点类节点上，那个数**永远见不了底**，
于是「三个数都见底」在结构上不可能成立。发现一道门永远过不去时，先量它的
两个端点，再决定是拆门还是修数。

现在门只数**有 id、有记账、分母到得了底**的两样：未揭开的线索配对、未触发的
一次性议程。缺数据（`None`）时不拦——那是"这份模组数不出来"，不是"还剩很多"，
局面块里已经如实写明。

## 反向门守的是不可逆的那一侧

判错的代价仍然不对称，只是没原来那么悬殊了（收尾落在可撤回的 `ending`）。
「故事是不是真的完了」是语义，代码做不了；这里只在**明显还没完**时拦住。

🔴 这也是它**不问人**的理由：`exec/30 §10.3` 里「agent 提议 → 房主确认」被
否掉了——房主也是玩家，他对剧本同样未知，那张卡片问的是「故事到这儿了吗」，
**需要读过剧本才答得了，而全桌按设计就没人读过**。不能把判断交给唯一没有
信息的一方。
"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.keeper.capabilities.closure.remaining import (
    unfired_agenda_count,
    unrevealed_pair_count,
)
from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.runtime.beat import happened_this_beat
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

#: 每拍一条的进展留痕。它有两个用途：可审计（这一拍到底算不算推进），
#: 以及给「一拍只计一次」当游标。
PROGRESS_EVENT = "keeper.progress"


async def _record_progress(
    deps: KeeperDeps, *, clues_revealed: bool, world_advanced: bool
) -> list[str]:
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

        # 进展的口径只收**代码确定性可判**的那几样：去了新地方、揭开了新线索、
        # 世界往前走了一步（议程触发 / 既成事实 / 悬而未决开合，见
        # `TurnFacts.world_advanced_this_turn`）。"剧情有没有推进"是语义，
        # 不进这个计数。
        #
        # 🔴 **`world_advanced` 是 2026-08-18 补的第三样。** 那一局 19 拍里
        # 目睹了枪杀、拿到主线线索、触发了绑架议程、开合了 4 条悬而未决，
        # 按原来两样的口径**一样都不算进展**，那个数一路涨到 15——而超过
        # `STALL_PUSH_THRESHOLD` 之后局面块会从"参考"升级成"本轮硬要求：
        # 给推力"。于是整局都在告诉模型"这桌人在原地打转"。
        advanced = bool(newly) or clues_revealed or world_advanced
        if advanced:
            current_state[STALLED_TURNS_KEY] = 0
        elif not await happened_this_beat(db, deps.room_id, PROGRESS_EVENT):
            # 🔴 **一拍只计一次**（2026-08-18）：一次玩家发言会引发多次裁决——
            # 每掷完一批骰子就有一次结算叙事，而它本身又是一次完整裁决。原来
            # 每次执行都 +1，于是**每次检定把这个数推高 2**，越认真检定的局
            # 越像在打转，而这个信号的语义恰恰是相反的那件事。
            current_state[STALLED_TURNS_KEY] = load_stalled_turns(current_state) + 1

        room.keeper_state = current_state
        if newly:
            await record_event(db, deps, "keeper.visited", {"node_ids": newly})
        # 🔴 这条事件**也是**上面那个「一拍只计一次」的游标：它落库之后，同一拍
        # 里后续的执行就能看见"这一拍已经算过了"。`record_event` 只写库不推 WS，
        # 所以它不进 `AMBIENT_WS_EVENTS` 那张逐个列出的表。
        await record_event(db, deps, PROGRESS_EVENT, {"advanced": advanced})
    return newly


async def execute_closure(
    deps: KeeperDeps, decision: BaseModel, facts: TurnFacts
) -> tuple[list[str], list[str]]:
    report: list[str] = []
    issues: list[str] = []

    try:
        newly = await _record_progress(
            deps,
            clues_revealed=bool(facts.clues_revealed_this_turn),
            world_advanced=bool(facts.world_advanced_this_turn),
        )
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

    unrevealed = unrevealed_pair_count(deps.module, keeper_state)
    if unrevealed:
        issues.append(f"自然收尾未执行：还有 {unrevealed} 条线索配对没揭开")
        return report, issues

    unfired = unfired_agenda_count(deps.module, keeper_state)
    if unfired:
        issues.append(f"自然收尾未执行：还有 {unfired} 条一次性议程没发生")
        return report, issues

    try:
        # 不带 ending_id：这一局没有命中任何**预设**结局，它是跑完了。
        # 记成 ending_id 会凭空造一个剧本里不存在的结局 id。
        report.append(await set_phase_impl(deps, PHASE_ENDING))
        report.append("故事进入收尾（无预设结局命中；玩家继续行动会退回调查阶段）")
    except KeeperToolError as exc:
        issues.append(f"自然收尾未执行：{exc}")
    return report, issues
