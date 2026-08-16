"""established 能力贡献给 `KeeperDecision` 的字段片段。"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from app.core.keeper.contract.registry import Capability, DecisionModel


class NewFact(DecisionModel):
    """一条既成事实：**已经发生、已经了结、后果永远为真**的东西。

    只收文本，id 由代码分配——同即兴地点与悬而未决，判据是「不要用自由文本
    当标识符」。
    """

    text: str = Field(
        description=(
            "写成**已经完成的事实**（「调查员烧掉了林中的木屋」），"
            "不是还在持续的处境（那个写 new_threads）"
        )
    )


class EstablishedDecisionFields(DecisionModel):
    """被 `KeeperDecision` 继承的字段片段。

    🔴 **故意没有 `resolved_facts`**：既成事实不会了结，这正是它跟
    `open_threads` 的分界。给它一个结清动作，模型迟早会用，那条记忆就没了。
    """

    new_facts: list[NewFact] = Field(default_factory=list)


FIELD_CAPABILITIES = {"new_facts": Capability.UPDATE_STATE}


def audit_fields(decision: BaseModel) -> Mapping[str, object]:
    return {"new_facts": len(getattr(decision, "new_facts", ()))}
