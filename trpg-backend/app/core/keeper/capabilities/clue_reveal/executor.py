"""clue_reveal 能力的执行层：把裁决里的 `clues_revealed` 记进已揭开列表。"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.keeper.capabilities.clue_reveal.pairs import (
    CLUES_REVEALED_KEY,
    ROOM_WIDE_OBSERVER,
    load_revealed_clues,
    serialize_revealed_clues,
)
from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.runtime.deps import KeeperDeps, KeeperToolError, record_event
from app.models.room import Room


async def mark_clues_revealed_impl(
    deps: KeeperDeps,
    pair_ids: list[str],
    *,
    room_wide: bool = True,
) -> str:
    """标记密级配对已揭开。默认房间级（@*）；幂等。"""
    if not pair_ids:
        return "密级揭开：（无）"

    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")

        current_state = dict(room.keeper_state or {})
        entries = load_revealed_clues(current_state)
        existing = set(entries)
        newly: list[str] = []
        observer = ROOM_WIDE_OBSERVER if room_wide else deps.player_id
        report: list[str] = []

        for pid in pair_ids:
            pair = next(
                (p for p in deps.module.visibility_pairs if p.id == pid),
                None,
            )
            if pair is None:
                raise KeeperToolError(f"剧本里没有 visibility_pair id={pid}")
            key = (pid, observer)
            if key in existing or (pid, ROOM_WIDE_OBSERVER) in existing:
                report.append(f"{pid}（已揭开）")
                continue
            entries.append(key)
            existing.add(key)
            newly.append(pid)
            report.append(pid)

        if newly:
            current_state[CLUES_REVEALED_KEY] = serialize_revealed_clues(entries)
            room.keeper_state = current_state
            await record_event(
                db,
                deps,
                "keeper.visibility",
                {"pair_ids": newly, "observer": observer},
            )

    return "密级揭开：" + "、".join(report) if report else "密级揭开：（无）"


async def execute_clues_revealed(
    deps: KeeperDeps, decision: BaseModel, _facts: TurnFacts
) -> tuple[list[str], list[str]]:
    """先校验 pair id 合法性，再交给 `mark_clues_revealed_impl`。

    白名单外的 id 一律跳过并记 issue——同 `agenda_fired`：模型编造的 id 不进状态。
    """
    revealed = list(getattr(decision, "clues_revealed", ()))
    if not revealed:
        return [], []
    report: list[str] = []
    issues: list[str] = []
    pair_ids_ok = {p.id for p in deps.module.visibility_pairs}
    valid_pairs: list[str] = []
    for pid in revealed:
        if pid not in pair_ids_ok:
            issues.append(f"密级揭开未执行：剧本里没有 pair id={pid}")
            continue
        valid_pairs.append(pid)
    if valid_pairs:
        try:
            report.append(await mark_clues_revealed_impl(deps, valid_pairs))
        except KeeperToolError as exc:
            issues.append(f"密级揭开未执行：{exc}")
    return report, issues
