"""madness 能力贡献给 `KeeperDecision` 的字段片段。"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from app.core.keeper.contract.registry import Capability, DecisionModel


class MadnessRecovery(DecisionModel):
    """一名调查员从临时性疯狂里缓过来了。

    🔴 **只有解除有字段，进入没有。** 进入的条件（单次理智损失 ≥5）是代码
    算出来的数、症状点数也是代码掷的，那一半一律代码强制（见
    `runtime/madness_state.py` 的说明）。给模型一个"让谁发疯"的字段就等于把
    已经确定的东西交回去重猜一遍。

    反过来，"他缓过来了没有"代码判不了，而**没有 schema 字段的状态出不来**
    ——`#46` 的隐匿当初只在 prompt 里写了"被发现要解除"，于是隐匿永不解除。
    这个字段就是那条教训的落点。
    """

    player: str = Field(description="调查员昵称或角色名，必须是局面块里标着「疯狂中」的人")
    reason: str = Field(default="", description="他为什么缓过来了，一句话（留痕用）")


class MadnessDecisionFields(DecisionModel):
    """被 `KeeperDecision` 继承的字段片段（见 `decision.py` 那行显式继承）。"""

    madness_recovered: list[MadnessRecovery] = Field(default_factory=list)


#: 单独一条权限，跟 `SET_HIDING` 同族：疯狂是**已经成立的状态**，不该因为
#: 这一轮玩家只是问了守秘人一句话就跟着"世界不推进"一起被收走。
FIELD_CAPABILITIES = {"madness_recovered": Capability.CLEAR_MADNESS}


def audit_fields(decision: BaseModel) -> Mapping[str, object]:
    return {
        "madness_recovered": [r.player for r in getattr(decision, "madness_recovered", ())],
    }
