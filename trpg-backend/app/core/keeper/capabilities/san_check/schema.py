"""san_check 能力贡献给 `KeeperDecision` 的字段片段。"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from app.core.keeper.contract.registry import Capability, DecisionModel


class SanCheckRequest(DecisionModel):
    player: str | None = None
    loss_on_success: str = "0"
    loss_on_failure: str = "1"
    reason: str = ""


class SanCheckDecisionFields(DecisionModel):
    """被 `KeeperDecision` 继承的字段片段（见 `decision.py` 那行显式继承）。"""

    san_checks: list[SanCheckRequest] = Field(default_factory=list)


FIELD_CAPABILITIES = {"san_checks": Capability.REQUEST_SAN_CHECK}


def audit_fields(decision: BaseModel) -> Mapping[str, object]:
    return {"san_checks": len(getattr(decision, "san_checks", ()))}
