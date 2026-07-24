"""裁决契约与执行器（keeper agent v2 · 两阶段回合制，两段式玩家掷骰）。

「裁决」是守秘人的幕后认知：这个行动要不要检定、状态怎么变。v1 把它做成
agent 的"自由工具调用"，实测被模型的写作本能碾压（该掷不掷/线索白给/状态
不记，三轮 prompt 强化无效）。v2 把裁决抬成**独立 LLM 调用的结构化输出**：
`KeeperDecision` 的字段就是裁决的完整词汇表——检定是 schema 的一部分而不是
"可选的工具"，不存在"忘了裁决"这条路径。

裁决产出分两条路径执行：
- `execute_side_effects`：HP 变更 + 状态记账，纯代码立即执行（LLM 摸不到
  骰子也改不了账）；
- `create_pending_checks`：checks/san_checks **不再在这里掷骰**——两段式
  玩家掷骰下，骰子由玩家在前端点击确认后才服务端权威生成，这里只把裁决
  产出的检定请求解析成待掷记录（`pending.PendingCheck`），真正的掷骰在
  `KeeperAgent.resolve_check` 里发生（见 agent.py）。
"""

import uuid

import structlog
from pydantic import BaseModel, ConfigDict, Field

from app.core.keeper.pending import PendingCheck
from app.core.keeper.phase import PHASE_FINISHED, PHASE_INVESTIGATION
from app.core.keeper.tools import (
    KeeperDeps,
    KeeperToolError,
    _resolve_character,
    _resolve_skill_target,
    adjust_hp_impl,
    mark_agenda_fired_impl,
    mark_visibility_revealed_impl,
    set_phase_impl,
    update_state_impl,
)

logger = structlog.get_logger()


class _DecisionModel(BaseModel):
    # 裁决 JSON 由 LLM 生成，多给的字段忽略、不报错——校验的重点是"必要的
    # 结构在"，不是"一个字都不能多"。
    model_config = ConfigDict(extra="ignore")


class CheckRequest(_DecisionModel):
    """一次技能/属性检定请求。player 为 None = 本轮行动的发起玩家。"""

    skill: str
    player: str | None = None
    reason: str = ""


class SanCheckRequest(_DecisionModel):
    player: str | None = None
    loss_on_success: str = "0"
    loss_on_failure: str = "1"
    reason: str = ""


class HpChange(_DecisionModel):
    delta: int
    player: str | None = None
    reason: str = ""


class StateUpdate(_DecisionModel):
    key: str
    value: str


class KeeperDecision(_DecisionModel):
    """裁决阶段的完整输出契约。

    所有列表字段默认空——"本轮不需要检定"表现为 `checks=[]` 加上 `thinking`
    里的理由，与 v1 的 declare_no_check 等价但更强：它不是模型"选择调用"的
    工具，而是每轮必然产出的结构化字段，天然可审计（structlog 落盘）。
    """

    thinking: str = Field(default="", description="裁决理由（审计用，不广播给玩家）")
    checks: list[CheckRequest] = Field(default_factory=list)
    san_checks: list[SanCheckRequest] = Field(default_factory=list)
    hp_changes: list[HpChange] = Field(default_factory=list)
    state_updates: list[StateUpdate] = Field(default_factory=list)
    agenda_fired: list[str] = Field(
        default_factory=list, description="本轮真正发生的议程事件 id（不预告）"
    )
    # 路线 5：本轮玩家挣得后可揭开的密级配对 id（须存在于 module.visibility_pairs）
    visibility_revealed: list[str] = Field(
        default_factory=list, description="本轮揭开的 visibility_pair id"
    )
    # 路线 6：开场仪式完成 → investigation；命中结局 → ending 收束
    opening_complete: bool = Field(default=False, description="开场仪式是否已完成（委托已建立等）")
    ending_reached: str | None = Field(default=None, description="本轮命中的结局 id；None=未收束")
    narration_guidance: str = Field(
        default="", description="给叙事阶段的指引：可揭示什么/须保密什么/NPC 如何反应"
    )


async def execute_side_effects(
    deps: KeeperDeps, decision: KeeperDecision
) -> tuple[list[str], list[str]]:
    """执行裁决里"立即生效"的部分：HP 变更 + 状态记账 + 议程触发。返回 (执行报告, 问题清单)。

    检定/理智检定不在这里执行——两段式玩家掷骰下骰子由玩家确认后才生成，
    见 `create_pending_checks`。

    - 执行报告：每项 `*_impl` 的完整返回文本，喂给叙事阶段——叙事者必须知道
      "发生了什么"才能如实写；
    - 问题清单：裁决里不合法的项（找不到的玩家 / 未知议程 id）**跳过不炸**，
      记下来一并喂给叙事阶段让它自然圆场；同时进日志供排查。

    议程 once 去重下沉在 mark_agenda_fired_impl（它拿得到 deps.module 与现值），
    这里只做「id 不存在 → issue」。

    执行是顺序的（不并发），tools 层的 write_lock 因此在这条路径上只是冗余
    保险——保留它是因为 `*_impl` 还可能被未来的并发调用方复用。
    """
    report: list[str] = []
    issues: list[str] = []

    for hp in decision.hp_changes:
        try:
            report.append(
                await adjust_hp_impl(deps, hp.delta, hp.reason or "守秘人裁定", hp.player)
            )
        except KeeperToolError as exc:
            issues.append(f"HP 变更未执行：{exc}")
    for update in decision.state_updates:
        try:
            report.append(await update_state_impl(deps, update.key, update.value))
        except KeeperToolError as exc:
            issues.append(f"状态更新未执行：{exc}")

    # 议程触发：只校验 id 合法性，once 幂等由 mark_agenda_fired_impl 兜底。
    if decision.agenda_fired:
        valid_ids: list[str] = []
        for eid in decision.agenda_fired:
            if deps.module.agenda_by_id(eid) is None:
                issues.append(f"议程事件未执行：剧本里没有 id={eid}")
                continue
            valid_ids.append(eid)
        if valid_ids:
            try:
                report.append(await mark_agenda_fired_impl(deps, valid_ids))
            except KeeperToolError as exc:
                issues.append(f"议程事件未执行：{exc}")

    # 密级配对揭开（路线 5）
    if decision.visibility_revealed:
        pair_ids_ok = {p.id for p in deps.module.visibility_pairs}
        valid_pairs: list[str] = []
        for pid in decision.visibility_revealed:
            if pid not in pair_ids_ok:
                issues.append(f"密级揭开未执行：剧本里没有 pair id={pid}")
                continue
            valid_pairs.append(pid)
        if valid_pairs:
            try:
                report.append(await mark_visibility_revealed_impl(deps, valid_pairs))
            except KeeperToolError as exc:
                issues.append(f"密级揭开未执行：{exc}")

    # 对局阶段推进（路线 6）
    if decision.ending_reached:
        eid = decision.ending_reached
        if deps.module.endings and not any(e.id == eid for e in deps.module.endings):
            issues.append(f"结局收束未执行：剧本里没有 ending id={eid}")
        else:
            try:
                # 收束当轮直接 finished：叙事仍可写终章，下一行动立即拒
                report.append(await set_phase_impl(deps, PHASE_FINISHED, ending_id=eid))
            except KeeperToolError as exc:
                issues.append(f"结局收束未执行：{exc}")
    elif decision.opening_complete:
        try:
            report.append(await set_phase_impl(deps, PHASE_INVESTIGATION))
        except KeeperToolError as exc:
            issues.append(f"开场完成未执行：{exc}")

    if issues:
        logger.warning("keeper_decision_issues", issues=issues)
    return report, issues


async def create_pending_checks(
    deps: KeeperDeps, decision: KeeperDecision
) -> tuple[list[PendingCheck], list[str]]:
    """把裁决里的 checks/san_checks 解析成待掷记录——**本函数不掷骰**。

    玩家/技能名的合法性预检复用 tools.py 内部的解析函数（跟 roll_check_impl/
    san_check_impl 走的是同一套解析逻辑，保证"能不能掷"的判断口径一致）；
    非法项跳过并记 issue（未知技能名、找不到的玩家），与旧版执行器行为一致。
    返回 (待掷记录, 问题清单)。
    """
    pending: list[PendingCheck] = []
    issues: list[str] = []

    async with deps.session_factory() as db:
        for check in decision.checks:
            try:
                player, character = await _resolve_character(db, deps, check.player)
                display_name, _target = _resolve_skill_target(deps, character, check.skill)
            except KeeperToolError as exc:
                issues.append(f"检定[{check.skill}]未能发起：{exc}")
                continue
            pending.append(
                PendingCheck(
                    check_request_id=str(uuid.uuid4()),
                    kind="skill",
                    room_id=deps.room_id,
                    player_id=player.id,
                    player_nickname=player.nickname,
                    skill=display_name,
                    loss_on_success="0",
                    loss_on_failure="0",
                    reason=check.reason,
                )
            )
        for san in decision.san_checks:
            try:
                player, _character = await _resolve_character(db, deps, san.player)
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

    if issues:
        logger.warning("keeper_pending_check_issues", issues=issues)
    return pending, issues
