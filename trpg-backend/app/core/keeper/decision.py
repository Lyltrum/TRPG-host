"""裁决契约与执行器（keeper agent v2 · 两阶段回合制）。

「裁决」是守秘人的幕后认知：这个行动要不要检定、状态怎么变。v1 把它做成
agent 的"自由工具调用"，实测被模型的写作本能碾压（该掷不掷/线索白给/状态
不记，三轮 prompt 强化无效）。v2 把裁决抬成**独立 LLM 调用的结构化输出**：
`KeeperDecision` 的字段就是裁决的完整词汇表——检定是 schema 的一部分而不是
"可选的工具"，不存在"忘了裁决"这条路径。

执行器（execute_decision）是纯代码：拿着裁决逐项调 tools.py 的 `*_impl`
（服务端权威掷骰/写库/留痕全部复用），LLM 摸不到骰子也改不了账。
"""

import structlog
from pydantic import BaseModel, ConfigDict, Field

from app.core.keeper.tools import (
    KeeperDeps,
    KeeperToolError,
    adjust_hp_impl,
    roll_check_impl,
    san_check_impl,
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
    narration_guidance: str = Field(
        default="", description="给叙事阶段的指引：可揭示什么/须保密什么/NPC 如何反应"
    )


async def execute_decision(
    deps: KeeperDeps, decision: KeeperDecision
) -> tuple[list[str], list[str]]:
    """逐项执行裁决。返回 (执行报告, 未能执行的问题)。

    - 执行报告：每项 `*_impl` 的完整返回文本（含困难/极难阈值、临时疯狂警告
      等），喂给叙事阶段——叙事者必须知道"骰子说了什么"才能如实写；
    - 问题清单：裁决里不合法的项（未知技能名、找不到的玩家）**跳过不炸**，
      记下来一并喂给叙事阶段让它自然圆场；同时进日志供排查。

    执行是顺序的（不并发），tools 层的 write_lock 因此在这条路径上只是冗余
    保险——保留它是因为 `*_impl` 还可能被未来的并发调用方复用。
    """
    report: list[str] = []
    issues: list[str] = []

    for check in decision.checks:
        try:
            report.append(await roll_check_impl(deps, check.skill, check.player))
        except KeeperToolError as exc:
            issues.append(f"检定[{check.skill}]未执行：{exc}")
    for san in decision.san_checks:
        try:
            report.append(
                await san_check_impl(deps, san.loss_on_success, san.loss_on_failure, san.player)
            )
        except KeeperToolError as exc:
            issues.append(f"理智检定未执行：{exc}")
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

    if issues:
        logger.warning("keeper_decision_issues", issues=issues)
    return report, issues
