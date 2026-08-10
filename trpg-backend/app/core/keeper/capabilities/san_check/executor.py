"""san_check 能力的执行层：理智检定的掷骰与结算。

掷骰本身在 `primitives/dice.py`（技能检定也用同一套）；待掷队列在
`keeper/pending.py`（两段式玩家掷骰的流程机制，两片检定共用）。这里只有
「理智检定怎么算、怎么写角色卡」这部分本能力独有的知识。
"""

from __future__ import annotations

import uuid

import structlog
from pydantic import BaseModel

from app.core.keeper.capabilities.san_check.state import (
    SAN_POINTS_FIRED_KEY,
    fired_refs_at,
    load_fired_san_points,
)
from app.core.keeper.contract.registry import PendingContext, TurnFacts
from app.core.keeper.primitives import dice
from app.core.keeper.runtime.deps import (
    KeeperDeps,
    KeeperToolError,
    current_stat,
    record_event,
    resolve_character,
    write_stat,
)
from app.core.keeper.runtime.location_state import location_of, resolve_content_node_id
from app.core.keeper.runtime.pending import PendingCheck
from app.core.narration.contract import CheckResultNotice
from app.models.room import Room

logger = structlog.get_logger()


async def san_check_detail(
    deps: KeeperDeps,
    loss_on_success: str,
    loss_on_failure: str,
    player_name: str | None = None,
) -> tuple[str, dict]:
    """理智检定的完整实现，额外返回结构化明细（同 `roll_check_detail`，供
    两段式玩家掷骰的 `san.check.result` 事件使用）。`san_check_impl` 是它的
    薄包装，保持旧签名不破坏现有调用方/测试。"""
    # write_lock：见 KeeperDeps 注释——并行工具调用下的读-改-写必须串行。
    async with deps.write_lock, deps.session_factory() as db:
        player, character = await resolve_character(db, deps, player_name)
        current = current_stat(character, "SAN")
        outcome = dice.evaluate_check(dice.roll_d100(deps.rng), current)
        loss_expr = loss_on_success if outcome.succeeded else loss_on_failure
        loss = max(0, dice.roll_dice_expr(loss_expr, deps.rng))
        new_value = max(0, current - loss)
        write_stat(character, "SAN", new_value)
        await record_event(
            db,
            deps,
            "keeper.san",
            {
                "player": player.nickname,
                "rolled": outcome.rolled,
                "target": current,
                "succeeded": outcome.succeeded,
                "loss": loss,
                "san": new_value,
            },
        )
    result = "成功" if outcome.succeeded else "失败"
    warnings = []
    if loss >= 5:
        warnings.append("单次损失≥5，触发临时疯狂（由你按 COC7 规则叙述发作表现）")
    if new_value == 0:
        warnings.append("理智归零，角色永久疯狂")
    suffix = "；".join(warnings)
    deps.check_results.append(
        f"{player.nickname} · 理智检定：{outcome.rolled}/{current} → {result}，"
        f"San {current} → {new_value}（-{loss}）"
    )
    text = (
        f"{player.nickname} 理智检定：d100={outcome.rolled}/{current} → {result}，"
        f"损失 {loss} 点（{loss_expr}），San {current} → {new_value}"
        + (f"。⚠️ {suffix}" if suffix else "")
    )
    detail = {
        "player_id": player.id,
        "player": player.nickname,
        "rolled": outcome.rolled,
        "target": current,
        "succeeded": outcome.succeeded,
        "loss": loss,
        "san": new_value,
    }
    return text, detail


async def san_check_impl(
    deps: KeeperDeps,
    loss_on_success: str,
    loss_on_failure: str,
    player_name: str | None = None,
) -> str:
    text, _detail = await san_check_detail(deps, loss_on_success, loss_on_failure, player_name)
    return text


async def create_pending_san_checks(
    deps: KeeperDeps, decision: BaseModel, context: PendingContext
) -> tuple[list[PendingCheck], list[str]]:
    """把裁决里的 `san_checks` 解析成待掷记录——**不掷骰**。

    玩家合法性预检复用 `resolve_character`（跟 `san_check_impl` 同一套解析
    逻辑，保证"能不能掷"的判断口径一致）；找不到的玩家跳过并记 issue。
    """
    pending: list[PendingCheck] = []
    issues: list[str] = []
    for san in getattr(decision, "san_checks", ()):
        try:
            player, _character = await resolve_character(context.db, deps, san.player)
        except KeeperToolError as exc:
            issues.append(f"理智检定未能发起：{exc}")
            continue
        pending.append(
            PendingCheck(
                check_request_id=str(uuid.uuid4()),
                kind="san",
                room_id=deps.room_id,
                player_id=player.id,
                player_nickname=player.nickname,
                skill=None,
                loss_on_success=san.loss_on_success,
                loss_on_failure=san.loss_on_failure,
                reason=san.reason,
            )
        )
    return pending, issues


async def mark_san_points_fired(
    deps: KeeperDeps, decision: BaseModel, _facts: TurnFacts
) -> tuple[list[str], list[str]]:
    """本轮发起过理智检定 → 把这些人所在节点上标注的检定点记为已触发。

    只为一件事：让局面块那条提醒**不再重复**。COC7 里同一来源不重复检定，而
    没有这笔记账的话，玩家在这个节点待几轮就会被提醒几轮（`exec/31 #73`）。

    刻意做得粗：按"发起检定的人此刻在哪"整节点标掉，不去对应"标注的是哪一条"
    ——`ModuleCheck` 没有 id，模型的 `san_checks` 里也没有指回标注的字段，
    真要精确对应只能拿理由文本去猜，那正是**不要用自由文本当标识符**。
    排在 movement（order 30）之后：要用的是本轮移动完成后的位置。
    """
    requests = list(getattr(decision, "san_checks", ()))
    if not requests:
        return [], []
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            return [], ["理智检定点记账未执行：房间不存在"]
        current_state = dict(room.keeper_state or {})
        already = load_fired_san_points(current_state)
        newly: list[str] = []
        for san in requests:
            try:
                player, _character = await resolve_character(db, deps, san.player)
            except KeeperToolError:
                # 发起侧（create_pending_san_checks）已经把这条记成 issue 了，
                # 这里不重复报，只是没有位置可记。
                continue
            # 记账要记在**内容节点**上（即兴地点沿 from 上溯），跟局面块同一
            # 口径——两边口径不一致就会"提醒了却记不掉"，玩家在屋后被反复提醒。
            node_id = resolve_content_node_id(
                deps.module, current_state, location_of(current_state, player.id)
            )
            if not node_id:
                continue
            for ref in fired_refs_at(deps.module, node_id):
                if ref not in already:
                    already.append(ref)
                    newly.append(ref)
        if newly:
            current_state[SAN_POINTS_FIRED_KEY] = ", ".join(already)
            room.keeper_state = current_state
            # 留痕**也是**这里唯一的 commit（record_event 负责提交，同 agenda）。
            await record_event(db, deps, "keeper.san_point", {"refs": newly})
    if not newly:
        return [], []
    return [f"模组标注的理智检定点已触发：{'、'.join(newly)}"], []


async def settle_san_check(deps: KeeperDeps, pending: PendingCheck) -> CheckResultNotice:
    """玩家点了掷骰之后：掷一次理智检定并写回角色卡。"""
    _text, detail = await san_check_detail(
        deps, pending.loss_on_success, pending.loss_on_failure, pending.player_nickname
    )
    return CheckResultNotice(
        check_request_id=pending.check_request_id,
        kind="san",
        player_id=detail["player_id"],
        skill=None,
        rolled=detail["rolled"],
        target=detail["target"],
        level="成功" if detail["succeeded"] else "失败",
        san_loss=detail["loss"],
        san_remaining=detail["san"],
    )
