"""health 能力的执行层：把裁决里的 `hp_changes` 变成真实的血量变化。

调查员走角色卡（`characters.derived_stats`），NPC 走 `keeper_state` 的 NPC
状态表（它没有角色卡可写）——两条记账各自自洽，见 `npc_state.py`。
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from app.core.coc7.rules import resolve_max_hp
from app.core.keeper.capabilities.health.npc_state import (
    NPC_STATE_KEY,
    apply_hp_delta,
    initial_hp,
    load_npc_states,
)
from app.core.keeper.capabilities.health.schema import HpChange
from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.primitives.npcs import npc_display_name, resolve_npc_id
from app.core.keeper.runtime.deps import (
    KeeperDeps,
    KeeperToolError,
    current_stat,
    is_room_member,
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
        # 🔴 上限跟着"治疗回血"（规则 3c）一起加：在此之前 delta 只可能是负数，
        # 没有上限也不会出事；一旦允许正数，第一次包扎就可能把 HP 顶到上限以上。
        #
        # 走 `resolve_max_hp` 而不是直接读 `derived_stats["HP_MAX"]`：那一格是
        # **keeper 第一次改 HP 时**才备份的，没受过伤的卡上根本没有它（而"先被
        # 治疗后受伤"恰恰是那种卡）。权威函数有按公式重算的第二来源。
        # 它返回 `None` 时**不封顶**，不拿假上限兜底——这张卡连属性都没有。
        hp_max = resolve_max_hp(character.derived_stats or {}, character.attributes or {})
        new_value = max(0, current + delta)
        if hp_max is not None:
            new_value = min(new_value, hp_max)
        write_stat(character, "HP", new_value)
        await record_event(
            db,
            deps,
            "keeper.hp",
            {"player": player.nickname, "delta": delta, "hp": new_value, "reason": reason},
        )
    status = "（已倒地/濒死）" if new_value == 0 else ""
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
            if await _target_is_npc(deps, hp):
                label = hp.npc or hp.player
                report.append(await adjust_npc_hp_impl(deps, hp.delta, reason, label))
            else:
                report.append(await adjust_hp_impl(deps, hp.delta, reason, hp.player))
        except KeeperToolError as exc:
            issues.append(f"HP 变更未执行：{exc}")
    return report, issues


async def _target_is_npc(deps: KeeperDeps, hp: HpChange) -> bool:
    """这一笔该走 NPC 记账还是调查员记账——**由代码查表判定**（`exec/20` §2.4）。

    `player` 与 `npc` 两边**都是白名单**（房间花名册 / 剧本 NPC 表），"这个名字
    属于哪一类"代码自己查得出来，不该靠裁决器判对。`exec/19` #39 的原始症状就是
    它把 NPC 名写进 `player`，`resolve_character` 找不到人 → 整笔血量记账丢失，
    NPC 的伤势只活在叙事文字里，下一轮裁决器无从得知它伤到什么程度。

    🔴 **裁决器的显式标注优先，只有它选的那一边查不到时才改判。** 玩家昵称恰好
    与剧本 NPC 同名时，那个标注就是唯一的消歧信息——这也是没有按 `exec/20` §2.4
    原方案把两个字段合并成一个 `target` 的原因：合并之后撞名就只能由代码静默
    挑一个。

    ⚠️ 改判**不进 `issues`**：`issues` 会随 `narrate_prose` 喂给叙事模型当作
    "这件事没发生"，而改判之后这笔账是**真记上了**的。把它报成失败会让叙事与
    事件表各说各话（`exec/19` #43 那一族的 bug，只能靠读事件表发现）。改判只进
    结构化日志。
    """
    if hp.npc:
        if resolve_npc_id(deps.module, hp.npc) is not None:
            return True
        # 剧本里没有这个 NPC——它可能其实是个调查员。
        async with deps.session_factory() as db:
            if await is_room_member(db, deps, hp.npc):
                logger.info(
                    "hp_target_reclassified", wrote="npc", resolved_as="player", label=hp.npc
                )
                return False
        # 两边都不认：照裁决器说的走，让 adjust_npc_hp_impl 抛出带登场 NPC 列表
        # 的那条错误（那条错误对模型纠正下一轮有用，别在这里吞掉）。
        return True

    # 没写 `npc`。`player` 为空 = 本轮发起者，没有歧义可言。
    if not hp.player:
        return False
    if resolve_npc_id(deps.module, hp.player) is None:
        return False
    async with deps.session_factory() as db:
        if await is_room_member(db, deps, hp.player):
            return False  # 撞名：以裁决器的显式标注为准
    logger.info("hp_target_reclassified", wrote="player", resolved_as="npc", label=hp.player)
    return True
