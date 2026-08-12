"""open_threads 能力的执行层：开一条、关一条。"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from app.core.keeper.capabilities.open_threads.state import (
    OPEN_THREADS_KEY,
    OPEN_THREADS_SEQ_KEY,
    OPEN_THREADS_SOFT_LIMIT,
    load_open_threads,
    load_thread_seq,
    next_thread_id,
)
from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.runtime.deps import KeeperDeps, record_event
from app.models.room import Room

logger = structlog.get_logger()


async def execute_open_threads(
    deps: KeeperDeps, decision: BaseModel, _facts: TurnFacts
) -> tuple[list[str], list[str]]:
    """先关后开，一次写库。

    **先关后开**：同一轮里"这件事了结了、但引出了新的一件"是常见形状，先开
    后关会让新开的那条在同一轮里被 id 撞掉（新 id 由表长决定）。
    """
    new_threads = list(getattr(decision, "new_threads", ()))
    resolved = [str(t).strip() for t in getattr(decision, "resolved_threads", ()) if str(t).strip()]
    if not new_threads and not resolved:
        return [], []

    report: list[str] = []
    issues: list[str] = []
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            return [], ["悬而未决记账未执行：房间不存在"]
        state = dict(room.keeper_state or {})
        table = load_open_threads(state)
        seq = load_thread_seq(state)

        for thread_id in resolved:
            entry = table.pop(thread_id, None)
            if entry is None:
                # 白名单外的 id 一律拒绝，与 NPC/节点/议程/密级一致——编造的 id
                # 不该悄悄变成"关掉了什么都没发生"。
                issues.append(f"了结未执行：没有 {thread_id!r} 这件悬而未决的事")
                continue
            report.append(f"「{entry['text']}」已了结（{thread_id}）")

        opened: list[dict] = []
        for thread in new_threads:
            text = (thread.text or "").strip()
            if not text:
                issues.append("悬而未决未记录：内容为空")
                continue
            thread_id, seq = next_thread_id(seq)
            table[thread_id] = {"text": text}
            opened.append({"id": thread_id, "text": text})
            report.append(f"悬而未决 +1：{thread_id}「{text}」")

        room.keeper_state = {**state, OPEN_THREADS_KEY: table, OPEN_THREADS_SEQ_KEY: seq}
        # 留痕**也是**这里唯一的 commit（record_event 负责提交，同 agenda）。
        await record_event(db, deps, "keeper.threads", {"opened": opened, "resolved": resolved})

    if len(table) > OPEN_THREADS_SOFT_LIMIT:
        # 只报，不裁剪：局面块必须全量列出，藏起来的模型看不见就会重开一条。
        logger.warning("keeper_open_threads_bloat", room_id=deps.room_id, count=len(table))
    return report, issues
