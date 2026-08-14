"""cast 能力的执行层：把「本轮台上有谁」记下来。

白名单校验走 `resolve_npc_id`（跟 hp_changes / state_updates 同一个解析器）：
编造的 id 不进状态，否则又回到自由文本当标识符。
"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.keeper.capabilities.cast.state import (
    ON_STAGE_KEY,
    load_on_stage,
    serialize_on_stage,
)
from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.primitives.npcs import resolve_npc_id
from app.core.keeper.runtime.deps import KeeperDeps, record_event
from app.models.room import Room


async def execute_cast(
    deps: KeeperDeps, decision: BaseModel, _facts: TurnFacts
) -> tuple[list[str], list[str]]:
    """整份覆盖，不是增量——字段的语义就是"此刻台上有谁"的快照。

    🔴 **字段缺席 ≠ 台上没人**：受限主体拿到的窄模型可能根本没有这个字段
    （`getattr` 探不到），那时什么都不做，而不是把台上清空。同族于
    `execute_hp_changes` 用 `getattr` 探字段的理由。
    """
    if not hasattr(decision, "npcs_on_stage"):
        return [], []
    raw = list(getattr(decision, "npcs_on_stage", ()))

    issues: list[str] = []
    resolved: list[str] = []
    for label in raw:
        npc_id = resolve_npc_id(deps.module, str(label).strip())
        if npc_id is None:
            issues.append(f"在场 NPC 未记录：剧本里没有「{label}」")
            continue
        if npc_id not in resolved:
            resolved.append(npc_id)

    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            return [], [*issues, "在场 NPC 未记录：房间不存在"]
        state = dict(room.keeper_state or {})
        if load_on_stage(state) == resolved:
            # 没变就不写、不留痕：台上的人多数轮次是不变的，每轮记一条事件
            # 会把事件流冲得没法读。
            return [], issues
        state[ON_STAGE_KEY] = serialize_on_stage(resolved)
        room.keeper_state = state
        await record_event(db, deps, "keeper.cast", {"npc_ids": resolved})

    # 🔴 **不进执行报告**：报告只装"世界变了什么"，而"台上有谁"是这一轮的
    # 输入快照，不是发生的事。往报告里多塞一行会当场打红别的能力那些
    # "本轮报告有几条"的断言（`exec/37` 踩过）。
    return [], issues
