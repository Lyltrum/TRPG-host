"""agenda 能力贡献给 `KeeperDecision` 的字段片段。"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from app.core.keeper.contract.registry import Capability, DecisionModel


class AgendaDecisionFields(DecisionModel):
    """被 `KeeperDecision` 继承的字段片段（见 `decision.py` 那行显式继承）。"""

    agenda_fired: list[str] = Field(
        default_factory=list, description="本轮真正发生的议程事件 id（不预告）"
    )


FIELD_CAPABILITIES = {"agenda_fired": Capability.FIRE_AGENDA}


def audit_fields(decision: BaseModel) -> Mapping[str, object]:
    """本轮触发了哪几条议程。记 id 而不是条数——议程是"世界自己动了一下"，
    复盘时要知道动的是哪一条。"""
    return {"agenda_fired": list(getattr(decision, "agenda_fired", ()))}
