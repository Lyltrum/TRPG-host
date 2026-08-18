"""把裁决写下的既成事实落库。"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from app.core.keeper.capabilities.established.state import (
    ESTABLISHED_KEY,
    ESTABLISHED_SEQ_KEY,
    ESTABLISHED_SOFT_LIMIT,
    load_established,
    load_fact_seq,
    next_fact_id,
)
from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.runtime.deps import KeeperDeps
from app.models.room import Room

logger = structlog.get_logger()


async def execute_established(
    deps: KeeperDeps, decision: BaseModel, facts: TurnFacts
) -> tuple[list[str], list[str]]:
    """新增既成事实。返回 (执行报告, issues)。

    🔴 **只增不删**：这一片没有结清动作（见 `schema.py`）。

    🔴 报告里**只报"世界变了什么"**：新记下一条永久事实算世界变化，所以进报告；
    但它不带 id——报告是给叙事者看的，id 是给模型下一轮引用的，混在一起会让
    叙事把 `fact-3` 写进散文里。
    """
    new_facts = list(getattr(decision, "new_facts", ()))
    if not new_facts:
        return [], []

    report: list[str] = []
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            return [], ["既成事实未记录：房间不存在"]
        state = dict(room.keeper_state or {})
        table = load_established(state)
        seq = load_fact_seq(state)
        for item in new_facts:
            text = (item.text or "").strip()
            if not text:
                continue
            fact_id, seq = next_fact_id(seq)
            table[fact_id] = {"text": text}
            report.append(f"记下既成事实：{text}")
        state[ESTABLISHED_KEY] = table
        state[ESTABLISHED_SEQ_KEY] = seq
        room.keeper_state = state
        await db.commit()

    # 记下一条永久后果 = 世界往前走了一步。`closure`(85) 拿它算「无进展轮数」
    # ——那个数原来只认「去新节点 / 揭新线索」，于是这一类推进全被算成打转。
    if report:
        facts.world_advanced_this_turn = True

    if len(table) > ESTABLISHED_SOFT_LIMIT:
        # 观测，不是限流：条数失控说明模型在拿这张表当便签本，那时该查的是
        # "它把什么塞进来了"，而不是给这张表加裁剪。
        logger.warning(
            "keeper_established_facts_growing",
            room_id=deps.room_id,
            count=len(table),
            limit=ESTABLISHED_SOFT_LIMIT,
        )
    return report, []
