"""cast 能力的裁决字段：本轮台上有哪些 NPC。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.keeper.contract.registry import Capability


class CastDecisionFields(BaseModel):
    npcs_on_stage: list[str] = Field(
        default_factory=list,
        description=(
            "本轮跟调查员在同一个场景里的 NPC id 列表（取自【登场 NPC】里的 id，"
            "不是名字、不许编）。**每轮都要给全**：这是「此刻台上有谁」的快照，"
            "不是增量——上一轮在、这一轮走了的，这次就别写进来。"
            "场景里一个 NPC 都没有就给空数组。"
        ),
    )


FIELD_CAPABILITIES = {"npcs_on_stage": Capability.SET_SCENE}


def audit_fields(decision: BaseModel) -> dict:
    return {"npcs_on_stage": list(getattr(decision, "npcs_on_stage", ()))}
