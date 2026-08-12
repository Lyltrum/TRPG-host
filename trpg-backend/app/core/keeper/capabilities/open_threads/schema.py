"""open_threads 能力贡献给 `KeeperDecision` 的字段片段。"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from app.core.keeper.contract.registry import Capability, DecisionModel


class NewThread(DecisionModel):
    """一件刚刚悬起来的事。

    🔴 **只给文本，不给 id**：id 由代码分配（`thread-N`），同 `new_location`。
    让模型自己起 id 就是「不要用自由文本当标识符」的复发——同一件事下一轮
    换个措辞就变成两条并存的记录。
    """

    text: str = Field(
        description=(
            "这件事是什么，一句话，写成**仍然成立的状态**"
            "（「米-戈仍在追击」而不是「米-戈追了上来」）"
        )
    )


class OpenThreadsDecisionFields(DecisionModel):
    """被 `KeeperDecision` 继承的字段片段（见 `decision.py` 那行显式继承）。"""

    new_threads: list[NewThread] = Field(default_factory=list)
    resolved_threads: list[str] = Field(
        default_factory=list,
        description=(
            "已经了结的事，填局面块「悬而未决的事」里列出的 thread-N id；不得编造不存在的 id"
        ),
    )


#: 开与关共用一条：它们是同一件事的两头，分成两条权限就会出现"能开不能关"
#: 这种半截状态——而那正是 `#46`（隐匿永不解除）的形状。
#:
#: 不被任何轮次撤销（同 `SET_HIDING`）：悬着的事是**已经成立的处境**，不因为
#: 这一轮玩家只是问了守秘人一句话就该凭空消失。
FIELD_CAPABILITIES = {
    "new_threads": Capability.TRACK_THREADS,
    "resolved_threads": Capability.TRACK_THREADS,
}


def audit_fields(decision: BaseModel) -> Mapping[str, object]:
    """开了几条、关了哪几条。开的那边记条数不记正文——正文在
    `keeper.thread_opened` 事件里逐条留痕了，这里是"本轮这片能力动没动手"。"""
    return {
        "new_threads": len(getattr(decision, "new_threads", ())),
        "resolved_threads": list(getattr(decision, "resolved_threads", ())),
    }
