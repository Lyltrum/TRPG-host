"""san_check 能力的执行层：理智检定的掷骰与结算。

掷骰本身在 `primitives/dice.py`（技能检定也用同一套）；待掷队列在
`keeper/pending.py`（两段式玩家掷骰的流程机制，两片检定共用）。这里只有
「理智检定怎么算、怎么写角色卡」这部分本能力独有的知识。
"""

from __future__ import annotations

import uuid

import structlog
from pydantic import BaseModel

from app.core.keeper.contract.registry import PendingContext
from app.core.keeper.primitives import dice
from app.core.keeper.runtime.deps import (
    KeeperDeps,
    KeeperToolError,
    current_stat,
    record_event,
    resolve_character,
    write_stat,
)
from app.core.keeper.runtime.pending import PendingCheck
from app.core.narration.contract import CheckResultNotice

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
