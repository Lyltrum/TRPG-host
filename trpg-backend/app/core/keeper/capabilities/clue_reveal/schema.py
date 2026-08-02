"""clue_reveal 能力贡献给 `KeeperDecision` 的字段片段。"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from app.core.keeper.contract.registry import Capability, DecisionModel


class ClueRevealDecisionFields(DecisionModel):
    """被 `KeeperDecision` 继承的字段片段（见 `decision.py` 那行显式继承）。

    字段名从 `visibility_revealed` 改成 `clues_revealed`（exec/27 三处撞名）：
    `visibility` 这个词同时被"潜行状态"和"线索密级"用着，读代码的人分不清。
    """

    # 路线 5：本轮玩家挣得后可揭开的密级配对 id（须存在于 module.visibility_pairs）
    clues_revealed: list[str] = Field(
        default_factory=list, description="本轮揭开的 visibility_pair id"
    )


FIELD_CAPABILITIES = {"clues_revealed": Capability.REVEAL_CLUE}


def audit_fields(decision: BaseModel) -> Mapping[str, object]:
    """记 id 而不是条数：线索是这个游戏的硬通货，复盘要知道揭开的是哪一条。"""
    return {"clues_revealed": list(getattr(decision, "clues_revealed", ()))}
