"""agenda 能力的执行层：把裁决里的 `agenda_fired` 记进已触发列表。"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.keeper.capabilities.agenda.state import AGENDA_FIRED_KEY, load_fired_agenda
from app.core.keeper.deps import KeeperDeps, KeeperToolError, record_event
from app.core.keeper.registry import TurnFacts
from app.models.room import Room


async def mark_agenda_fired_impl(deps: KeeperDeps, event_ids: list[str]) -> str:
    """把议程事件标记为已触发（幂等：已在列表里且 once=True 的忽略）。

    once 语义必须由代码保证：LLM 的 state_updates 靠不住，实测多数轮不记。
    once=False 的事件允许重复触发——仍写事件留痕，但不重复塞进列表。

    必须走 write_lock（与 update_state_impl 一致——JSON 列是整体重新赋值，
    读改写并发会丢更新，v1 冒烟真的踩过）。
    """
    if not event_ids:
        return "议程事件触发：（无）"

    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")

        current_state = dict(room.keeper_state or {})
        already = load_fired_agenda(current_state)
        newly: list[str] = []
        report_parts: list[str] = []

        for eid in event_ids:
            event = deps.module.agenda_by_id(eid)
            title = (event.title if event is not None else None) or eid
            # once=True 且已在列表 → 幂等跳过（不是错误，只是不重复记账）
            if event is not None and event.once and eid in already:
                report_parts.append(f"{eid}（{title}，已触发过）")
                continue
            # once=False 或首次：进列表（once=False 已在列表里时不重复塞）
            if eid not in already:
                already.append(eid)
                newly.append(eid)
            report_parts.append(f"{eid}（{title}）")

        if newly:
            # ⚠️ JSON 列整体重新赋值（同 update_state_impl / _write_stat）。
            current_state[AGENDA_FIRED_KEY] = ", ".join(already)
            room.keeper_state = current_state
            await record_event(db, deps, "keeper.agenda", {"event_ids": newly})
        # 纯跳过（全部已触发过）时不写库、不留痕，但返回可读报告让调用方知情。

    if not report_parts:
        return "议程事件触发：（无）"
    return "议程事件触发：" + "、".join(report_parts)


async def execute_agenda_fired(
    deps: KeeperDeps, decision: BaseModel, _facts: TurnFacts
) -> tuple[list[str], list[str]]:
    """注册进执行阶段的钩子：先校验 id 合法性，再交给 `mark_agenda_fired_impl`。

    once 幂等下沉在 impl 里（它拿得到 `deps.module` 与现值），这里只做
    「id 不存在 → issue」。
    """
    fired = list(getattr(decision, "agenda_fired", ()))
    if not fired:
        return [], []
    report: list[str] = []
    issues: list[str] = []
    valid_ids: list[str] = []
    for eid in fired:
        if deps.module.agenda_by_id(eid) is None:
            issues.append(f"议程事件未执行：剧本里没有 id={eid}")
            continue
        valid_ids.append(eid)
    if valid_ids:
        try:
            report.append(await mark_agenda_fired_impl(deps, valid_ids))
        except KeeperToolError as exc:
            issues.append(f"议程事件未执行：{exc}")
    return report, issues
