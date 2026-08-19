"""san_check 能力的执行层：理智检定的掷骰与结算。

掷骰本身在 `primitives/dice.py`（技能检定也用同一套）；待掷队列在
`keeper/pending.py`（两段式玩家掷骰的流程机制，两片检定共用）。这里只有
「理智检定怎么算、怎么写角色卡」这部分本能力独有的知识。
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.keeper.capabilities.san_check.state import (
    RECENT_SAN_KEY,
    SAN_POINTS_FIRED_KEY,
    load_fired_san_points,
    load_recent_san_reasons,
    match_san_point,
    record_san_reason,
)
from app.core.keeper.contract.registry import PendingContext, TurnFacts
from app.core.keeper.primitives import dice
from app.core.keeper.runtime.beat import happened_this_beat
from app.core.keeper.runtime.deps import (
    KeeperDeps,
    KeeperToolError,
    current_stat,
    record_event,
    resolve_character,
    write_stat,
)
from app.core.keeper.runtime.madness_state import MADNESS_LOSS_THRESHOLD, enter_madness
from app.core.keeper.runtime.pending import PendingDecision
from app.core.narration.contract import CheckResultNotice
from app.models.room import Room

logger = structlog.get_logger()


async def san_check_detail(
    deps: KeeperDeps,
    loss_on_success: str,
    loss_on_failure: str,
    player_name: str | None = None,
) -> tuple[str, dict]:
    """理智检定的完整实现（掷骰 + 立刻生效），额外返回结构化明细。

    两段式玩家掷骰那条路**不走这里**（要的是「掷完先广播、再生效」），见
    `san_check_only` / `settle_san_check`。这里留给守秘人直接掷的场合，
    也是现有测试注入的接缝之一，签名与行为都不动。
    """
    text, detail = await san_check_only(deps, loss_on_success, loss_on_failure, player_name)
    await _apply_san_loss(deps, detail, player_name)
    return text, detail


async def san_check_only(
    deps: KeeperDeps,
    loss_on_success: str,
    loss_on_failure: str,
    player_name: str | None = None,
) -> tuple[str, dict]:
    """**只掷骰，不生效**：扣理智、记事件、给叙事的那句话都在 `_apply_san_loss`。

    为什么必须拆见 `SettleHook`。SAN 检定按规则不许花幸运，但它的形状必须
    跟技能检定一致——否则又是那条「同一件事的两头，一头可插拔一头写死」。
    """
    async with deps.session_factory() as db:
        player, character = await resolve_character(db, deps, player_name)
        current = current_stat(character, "SAN")
    outcome = dice.evaluate_check(dice.roll_d100(deps.rng), current)
    loss_expr = loss_on_success if outcome.succeeded else loss_on_failure
    loss = max(0, dice.roll_dice_expr(loss_expr, deps.rng))
    new_value = max(0, current - loss)
    result = "成功" if outcome.succeeded else "失败"
    warnings = []
    if loss >= 5:
        # 症状不在这里说：生效那一步（`_apply_san_loss`）会掷症状表并把具体
        # 那一条写进疯狂状态，两处都说会让叙事收到两种发作表现。
        warnings.append("单次损失≥5，触发临时疯狂")
    if new_value == 0:
        warnings.append("理智归零，角色永久疯狂")
    suffix = "；".join(warnings)
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


async def _apply_san_loss(deps: KeeperDeps, detail: dict, player_name: str | None) -> None:
    """生效：扣理智 + 记事件 + 给叙事留一句话。**只吃 detail**，理由见 `SettleHook`。"""
    loss = int(detail["loss"])
    current = int(detail["target"])
    # write_lock：见 KeeperDeps 注释——并行工具调用下的读-改-写必须串行。
    # 🔴 这里**重新读一次角色卡**再扣，不写掷骰那一刻算出的 `san`：掷骰与生效
    # 之间隔着一次广播（幸运消费还隔着玩家的决定），期间别处改过 SAN 的话，
    # 照旧值写回去就是把那次改动吞掉。
    async with deps.write_lock, deps.session_factory() as db:
        player, character = await resolve_character(db, deps, player_name)
        # 疯狂那一步要用它，而 `detail` 里**不一定有** player_id：两段式那条路
        # 传进来的 detail 是 `apply_san_check` 现搭的（只有落过库的那几样）。
        player_id, nickname = player.id, player.nickname
        written = max(0, current_stat(character, "SAN") - loss)
        write_stat(character, "SAN", written)
        await record_event(
            db,
            deps,
            "keeper.san",
            {
                "player": player.nickname,
                "rolled": detail["rolled"],
                "target": current,
                "succeeded": detail["succeeded"],
                "loss": loss,
                "san": written,
            },
        )
    # 🔴 临时性疯狂：**代码强制**，不是请模型自觉。触发条件是这里刚算出来的
    # 数，症状点数也由代码掷——能确定性判断的一律代码强制。解除那一半才是
    # 模型的活儿（`capabilities/madness`）。状态放在 runtime 是因为两片能力
    # 不许互相 import，理由见 `madness_state` 模块说明。
    if loss >= MADNESS_LOSS_THRESHOLD:
        await enter_madness(deps, player_id, nickname)


async def san_check_impl(
    deps: KeeperDeps,
    loss_on_success: str,
    loss_on_failure: str,
    player_name: str | None = None,
) -> str:
    text, _detail = await san_check_detail(deps, loss_on_success, loss_on_failure, player_name)
    return text


async def san_already_rolled_this_beat(db: AsyncSession, room_id: str) -> bool:
    """这一句玩家发言引发的链条里，是不是已经掷过理智了。

    「一拍」的定义与判断下沉在 `runtime/beat.py`——`closure` 的「无进展轮数」
    后来也要同一条分界线，各写各的就是「同一件事有两种做法」。
    """
    return await happened_this_beat(db, room_id, "keeper.san")


async def create_pending_san_checks(
    deps: KeeperDeps, decision: BaseModel, context: PendingContext
) -> tuple[list[PendingDecision], list[str]]:
    """把裁决里的 `san_checks` 解析成待掷记录——**不掷骰**。

    玩家合法性预检复用 `resolve_character`（跟 `san_check_impl` 同一套解析
    逻辑，保证"能不能掷"的判断口径一致）；找不到的玩家跳过并记 issue。

    ## 🔴 一拍之内只掷一次理智（2026-08-16 真机，`exec/38 #86`）

    实测：玩家说了一句「我打开手电筒看一下里面有什么」之后**一个字都没再说**，
    系统跑了 3 次裁决、3 次理智检定，第三次 90/63 失败扣 6 点，**当场触发一次
    本不该有的临时性疯狂**。

    规则 3 里「同一来源不重复检定」**是写着的**，模型没遵守——但根因不是它不
    听话：**"已经为这个来源掷过了"这条信息从来没进过它的上下文。**模组标注的
    理智检定点有记账（`SAN_POINTS_FIRED_KEY`），而模型**自己判断**该掷的那些
    记账是零。这一局的模组标注恰好是 0 条 ⇒ 全部走自判 ⇒ 全部没有记账。
    「有消费方但没有数据」的又一处。

    🔴 **门不去认「来源」**：`reason` 是自由文本，「目击曼-巴加里」和「那东西
    又靠近了」认不出是同一个来源——那正是「不要用自由文本当标识符」。而实测
    数据给了一条不用认来源的分界线：**4 次检定里该掷的 2 次各自跟在一句新的
    玩家发言后面，失控的 2 次都是同一句话引发的第 2、3 次裁决。**按这条拦，
    真实数据上误伤为零。

    规则里那条「除非情境升级」的豁免也是这么被绕过的：**"它又朝你挪近半尺"
    正是模型自己上一拍写的叙事**——它拿自己的输出满足自己的例外条件，那个
    条件永远成立。判据用了一个模型自己能左右的量。

    代价很小：拦掉之后玩家下一次发言就能再掷（真的升级了，下一拍照样掷得出），
    而放过去的代价是一次凭空的疯狂。同一轮裁决里**多个来源**照旧可以各掷一次
    （拦的是链条上的第二次裁决，不是同一次裁决里的第二条）。
    """
    requests = list(getattr(decision, "san_checks", ()))
    if requests and await san_already_rolled_this_beat(context.db, deps.room_id):
        return [], [
            f"这一拍已经掷过理智检定了，本次的 {len(requests)} 次不再发起"
            "（同一来源不重复检定；玩家下一次行动之后可以再掷）"
        ]

    pending: list[PendingDecision] = []
    issues: list[str] = []
    for san in requests:
        try:
            player, _character = await resolve_character(context.db, deps, san.player)
        except KeeperToolError as exc:
            issues.append(f"理智检定未能发起：{exc}")
            continue
        pending.append(
            PendingDecision.roll(
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
    await _remember_san_reasons(deps, pending)
    return pending, issues


async def _remember_san_reasons(deps: KeeperDeps, pending: list[PendingDecision]) -> None:
    """记下这一批检定各是为什么掷的，供下一拍的局面块用（判据见 `state.py`）。

    🔴 **记的是真正入队的那些**，不是裁决里请求的那些：被上面那道「一拍只掷
    一次」的门拦掉的请求，玩家一眼都没看见，把它当成"已经掷过"会让下一拍的
    提醒指向一件没发生的事。
    """
    if not pending:
        return
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            return
        state = dict(room.keeper_state or {})
        recent = load_recent_san_reasons(state)
        for item in pending:
            recent = record_san_reason(recent, item.player_id, item.reason)
        state[RECENT_SAN_KEY] = recent
        room.keeper_state = state
        await db.commit()


async def mark_san_points_fired(
    deps: KeeperDeps, decision: BaseModel, _facts: TurnFacts
) -> tuple[list[str], list[str]]:
    """本轮发起过理智检定 → 把对应的模组标注记为已触发。

    只为一件事：让局面块那条提醒**不再重复**。COC7 里同一来源不重复检定，而
    没有这笔记账的话，玩家会被提醒到这一局结束（`exec/31 #73`）。

    🔴 **2026-08-15：口径从「按玩家所在节点整节点标掉」改成「按损失数值回匹」。**

    必须跟着注入一起改。注入改成全局列出之后，原来那套几乎永远是空操作
    ——标注挂在遭遇节点上，玩家站不上去，于是**标不掉、每轮重复**，模型照做
    就是重复扣 SAN，比不提醒更糟。「加了字段没有消费方」是一种缺陷，
    **改了口径只改一半**是它的镜面版本，两边都不会变红。

    数值可以当回匹依据是因为两侧同源：局面块把模组的数值原样列出，规则要求
    模型**照抄**。匹配不上就不标 + 记 issue（显式降级），不按顺序随便标一条
    ——多条标注数值不同时标错等于把另一条也吞掉。
    """
    requests = list(getattr(decision, "san_checks", ()))
    if not requests:
        return [], []
    issues: list[str] = []
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            return [], ["理智检定点记账未执行：房间不存在"]
        current_state = dict(room.keeper_state or {})
        already = load_fired_san_points(current_state)
        newly: list[str] = []
        for san in requests:
            # 匹配要拿**当前这一轮已经标掉的**当基线，否则同一轮里两次同数值的
            # 检定会匹配到同一条标注上。
            probe_state = {**current_state, SAN_POINTS_FIRED_KEY: ", ".join(already)}
            ref = match_san_point(
                deps.module, probe_state, san.loss_on_success, san.loss_on_failure
            )
            if ref is None:
                issues.append(
                    f"理智检定点未记账：损失 {san.loss_on_success}/{san.loss_on_failure} "
                    "对不上模组标注的任何一条（这次检定照常进行，只是不计入标注）"
                )
                continue
            already.append(ref)
            newly.append(ref)
        if newly:
            current_state[SAN_POINTS_FIRED_KEY] = ", ".join(already)
            room.keeper_state = current_state
            # 留痕**也是**这里唯一的 commit（record_event 负责提交，同 agenda）。
            await record_event(db, deps, "keeper.san_point", {"refs": newly})
    if not newly:
        return [], issues
    return [f"模组标注的理智检定点已触发：{'、'.join(newly)}"], issues


async def settle_san_check(deps: KeeperDeps, pending: PendingDecision) -> CheckResultNotice:
    """玩家点了掷骰之后：掷一次理智检定。扣卡与记账在 `apply_san_check` 里。"""
    _text, detail = await san_check_only(
        deps, pending.loss_on_success, pending.loss_on_failure, pending.player_nickname
    )
    return CheckResultNotice(
        check_request_id=pending.decision_id,
        kind="san",
        player_id=detail["player_id"],
        skill=None,
        rolled=detail["rolled"],
        target=detail["target"],
        level="成功" if detail["succeeded"] else "失败",
        san_loss=detail["loss"],
        san_remaining=detail["san"],
    )


async def apply_san_check(
    deps: KeeperDeps, pending: PendingDecision, notice: CheckResultNotice
) -> None:
    """把理智检定的结果落到角色卡上。输入只有落过库的那两样（见 `SettleHook`）。"""
    assert notice.san_loss is not None
    await _apply_san_loss(
        deps,
        {
            "player": pending.player_nickname,
            "rolled": notice.rolled,
            "target": notice.target,
            "succeeded": notice.level == "成功",
            "loss": notice.san_loss,
        },
        pending.player_nickname,
    )
