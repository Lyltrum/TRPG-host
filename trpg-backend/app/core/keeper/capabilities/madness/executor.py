"""madness 能力的执行层：解除疯狂。

**进入不在这里**——它由 `capabilities/san_check` 在理智损失落卡的那一刻代码
强制触发，两片能力共用 `runtime/madness_state.py`。理由见那个模块的说明。
"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.runtime.deps import KeeperDeps, KeeperToolError, resolve_character
from app.core.keeper.runtime.madness_state import clear_madness, load_madness, symptom_by_id
from app.models.room import Room


async def execute_madness_recovered(
    deps: KeeperDeps, decision: BaseModel, _facts: TurnFacts
) -> tuple[list[str], list[str]]:
    """把裁决里的 `madness_recovered` 落成真正的解除。"""
    requests = list(getattr(decision, "madness_recovered", ()))
    if not requests:
        return [], []
    issues: list[str] = []
    targets: list[tuple[str, str]] = []
    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        madness = load_madness(room.keeper_state if room is not None else None)
        for recovery in requests:
            try:
                player, _character = await resolve_character(db, deps, recovery.player)
            except KeeperToolError as exc:
                issues.append(f"疯狂解除未执行：{exc}")
                continue
            if player.id not in madness:
                # 「写了 ≠ 变了」：他本来就没在疯，报告里不许出现"他缓过来了"。
                issues.append(f"疯狂解除未执行：{player.nickname} 并不在疯狂中")
                continue
            targets.append((player.id, player.nickname))
    if not targets:
        return [], issues
    cleared = set(await clear_madness(deps, [pid for pid, _name in targets]))
    reports = [
        f"{name} 从「{_symptom_label(deps, madness[pid])}」中缓了过来，不再处于疯狂状态"
        for pid, name in targets
        if pid in cleared
    ]
    return reports, issues


def _symptom_label(deps: KeeperDeps, symptom_id: str) -> str:
    """症状名。查不到就用 id——这一句是**给叙事看的报告文本**，不是标识符，
    此处退化成 id 不会让任何判断走错（记录本身已经被删掉了）。"""
    symptom = symptom_by_id(deps.ruleset, symptom_id)
    return symptom.label if symptom is not None else symptom_id
