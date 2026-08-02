"""progression 能力贡献给 `KeeperDecision` 的字段片段。"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from app.core.keeper.contract.registry import Capability, DecisionModel


class ProgressionDecisionFields(DecisionModel):
    """被 `KeeperDecision` 继承的字段片段（见 `decision.py` 那行显式继承）。

    两个字段都是「该不该推进对局阶段」的裁决。阶段值本身归 runtime（见
    `phase.py` 的模块说明）——这里只管**什么时候推**。
    """

    # 路线 6：开场仪式完成 → investigation；命中结局 → ending 收束
    opening_complete: bool = Field(default=False, description="开场仪式是否已完成（委托已建立等）")
    ending_reached: str | None = Field(default=None, description="本轮命中的结局 id；None=未收束")


FIELD_CAPABILITIES = {
    "opening_complete": Capability.ADVANCE_PHASE,
    "ending_reached": Capability.ADVANCE_PHASE,
}


def audit_fields(decision: BaseModel) -> Mapping[str, object]:
    """收束与开场推进是整局里最要紧的两次状态跳变，逐次留痕。"""
    return {
        "opening_complete": bool(getattr(decision, "opening_complete", False)),
        "ending_reached": getattr(decision, "ending_reached", None),
    }
