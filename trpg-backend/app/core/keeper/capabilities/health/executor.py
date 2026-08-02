"""health 能力的执行层：把裁决里的 `hp_changes` 变成真实的血量变化。

调查员走角色卡（`characters.derived_stats`），NPC 走 `keeper_state` 的 NPC
状态表（它没有角色卡可写）——两条记账各自自洽，见 `npc_state.py`。
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from app.core.keeper.capabilities.health.npc_state import (
    NPC_STATE_KEY,
    apply_hp_delta,
    initial_hp,
    load_npc_states,
)
from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.primitives.npcs import npc_display_name, resolve_npc_id
from app.core.keeper.runtime.deps import (
    KeeperDeps,
    KeeperToolError,
    current_stat,
    record_event,
    resolve_character,
    write_stat,
)
from app.core.narration.contract import StatChangeNotice
from app.models.room import Room

logger = structlog.get_logger()


async def adjust_hp_impl(
    deps: KeeperDeps, delta: int, reason: str, player_name: str | None = None
) -> str:
    # write_lock：见 KeeperDeps 注释——并行工具调用下的读-改-写必须串行。
    async with deps.write_lock, deps.session_factory() as db:
        player, character = await resolve_character(db, deps, player_name)
        current = current_stat(character, "HP")
        new_value = max(0, current + delta)
        write_stat(character, "HP", new_value)
        await record_event(
            db,
            deps,
            "keeper.hp",
            {"player": player.nickname, "delta": delta, "hp": new_value, "reason": reason},
        )
    status = "（已倒地/濒死）" if new_value == 0 else ""
    deps.check_results.append(f"{player.nickname} · HP {current} → {new_value}{status}")
    deps.stat_changes.append(
        StatChangeNotice(
            player_id=player.id,
            hp=new_value,
            hp_max=character.derived_stats.get("HP_MAX") if character.derived_stats else None,
            reason=reason,
        )
    )
    return f"{player.nickname} HP {current} → {new_value}{status}（{reason}）"


async def adjust_npc_hp_impl(deps: KeeperDeps, delta: int, reason: str, npc_label: str) -> str:
    """给 NPC 记一笔生命值变化（exec/19 #39）。

    NPC 没有角色卡可写，状态挂在 `keeper_state` 的 NPC 状态表上。名字必须能
    解析成模组里的 npc id（白名单，见 `npc_state.resolve_npc_id`）——解析不出
    就报错让上层记 issue，**不新建条目**：凭裁决器随口写的名字建状态，等于
    让自由文本当标识符，下一轮换个称呼就成了两个 NPC。
    """
    npc_id = resolve_npc_id(deps.module, npc_label)
    if npc_id is None:
        known = "、".join(n.name for n in deps.module.npcs) or "（剧本没有登场 NPC）"
        raise KeeperToolError(f"剧本里没有 NPC「{npc_label}」。登场 NPC：{known}")
    # write_lock：见 KeeperDeps 注释——读-改-写必须串行。
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = dict(room.keeper_state or {})
        states = load_npc_states(current_state)
        state = apply_hp_delta(states, npc_id, delta, base_hp=initial_hp(deps.module, npc_id))
        current_state[NPC_STATE_KEY] = states
        # ⚠️ JSON 列整体重新赋值（同 write_stat 的原因）。
        room.keeper_state = current_state
        await record_event(
            db,
            deps,
            "keeper.npc_hp",
            {"npc": npc_id, "delta": delta, "state": state, "reason": reason},
        )
    name = npc_display_name(deps.module, npc_id)
    if isinstance(state.get("hp"), int):
        hp = state["hp"]
        status = "（已倒地/失去行动力）" if hp == 0 else ""
        summary = f"{name} HP → {hp}{status}"
    else:
        summary = f"{name} 累计受伤 {state.get('damage', 0)} 点（数据卡无 HP）"
    deps.check_results.append(summary)
    return f"{summary}（{reason}）"


async def execute_hp_changes(
    deps: KeeperDeps, decision: BaseModel, _facts: TurnFacts
) -> tuple[list[str], list[str]]:
    """注册进执行阶段的钩子。

    `decision` 标成 `BaseModel` 而不是 `KeeperDecision`：受限主体拿到的是
    `build_decision_model` 现造的窄模型，**确实不是** KeeperDecision。它有没有
    `hp_changes` 由权限决定，所以这里用 `getattr` 探——没有这个字段就等于
    这个主体无权做这件事，本轮什么都不做。
    """
    report: list[str] = []
    issues: list[str] = []
    for hp in getattr(decision, "hp_changes", ()):
        try:
            reason = hp.reason or "守秘人裁定"
            # NPC 与调查员走两条记账（exec/19 #39）：NPC 的状态挂在 keeper_state
            # 的 NPC 状态表上，没有角色卡可写。`npc` 优先——两个字段都填时以
            # 显式的 NPC 为准，因为 `player` 的默认语义是"本轮发起者"。
            if hp.npc:
                report.append(await adjust_npc_hp_impl(deps, hp.delta, reason, hp.npc))
            else:
                report.append(await adjust_hp_impl(deps, hp.delta, reason, hp.player))
        except KeeperToolError as exc:
            issues.append(f"HP 变更未执行：{exc}")
    return report, issues
