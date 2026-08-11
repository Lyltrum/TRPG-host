"""closure 能力贡献给 `KeeperDecision` 的字段片段。"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from app.core.keeper.contract.registry import Capability, DecisionModel


class ClosureDecisionFields(DecisionModel):
    """被 `KeeperDecision` 继承的字段片段（见 `decision.py` 那行显式继承）。

    与 `progression.ending_reached` 的分工：那个是**模组给的候选**命中了
    （`endings[].trigger` 满足），这个是**模组根本没给候选**时的自然收尾。

    为什么需要它：真人 KP 收尾时做两件事——判断"没有更多内容了" + 主动制造
    一个停得住的地方。agent 此前只有前者的一半（判 trigger），**没有"生产"
    这个动作**，所以模组没有 `endings` 时它不是判断失败，是根本没有动作可做。
    `exec/19 #47` 实测过后果：叙事已经完整写出了结局（FBI 封锁、烧掉房子），
    而 `keeper.phase` 仍是 investigation——**这个动作已经在发生，只是没有字段
    接住，漏在散文里**（「schema 表达不了的东西会从叙事里漏出去」）。

    🔴 **收尾那一"推"不做成显式动作**：怎么把故事停下来是叙事的事，交给高温
    那一拍自然写。这里只表达"该停了"。
    """

    story_ran_its_course: bool = Field(
        default=False,
        description=(
            "故事自然收尾：内容已经跑完、没有更多线索/事件/地方可去，可以落幕。"
            "**模组有 endings 时优先用 ending_reached**；本字段是给没有预设结局的"
            "模组用的。还在揭开新线索或还有未触发的议程时不许写 true。"
        ),
    )


FIELD_CAPABILITIES = {"story_ran_its_course": Capability.ADVANCE_PHASE}


def audit_fields(decision: BaseModel) -> Mapping[str, object]:
    """收束是整局最不可撤回的一次状态跳变，每轮都要留痕（含没收的那些轮）。"""
    return {"story_ran_its_course": bool(getattr(decision, "story_ran_its_course", False))}
