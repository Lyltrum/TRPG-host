"""inventory 能力的裁决字段：这一轮谁拿到/失去了什么东西。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.keeper.contract.registry import Capability, DecisionModel


class EquipmentChange(DecisionModel):
    """一次随身物品的增减。

    🔴 **增量，不是快照**（跟 `cast` 刻意相反）：随身清单是玩家自己写的东西，
    每轮让模型重报一遍全量，它迟早会漏掉一件、或者把玩家写的措辞改掉。
    HP 也是这个理由走 delta。
    """

    player: str | None = Field(
        default=None, description="调查员昵称或角色名；留空 = 本轮行动的发起玩家"
    )
    gained: list[str] = Field(
        default_factory=list, description="这一轮拿到的东西（一件一条，写玩家听得懂的名字）"
    )
    lost: list[str] = Field(
        default_factory=list,
        description="这一轮失去的东西（被夺走/用光/损毁）。名字要跟随身清单上的**一字不差**",
    )
    reason: str = Field(default="", description="怎么拿到/怎么没的，一句话")


class InventoryDecisionFields(DecisionModel):
    """被 `KeeperDecision` 继承的字段片段（见 `decision.py` 那行显式继承）。"""

    equipment_changes: list[EquipmentChange] = Field(default_factory=list)


#: 随身物品是角色卡上的东西，跟改 HP 同一类权限。
FIELD_CAPABILITIES = {"equipment_changes": Capability.CHANGE_EQUIPMENT}


def audit_fields(decision: BaseModel) -> dict:
    """记**改了什么**而不是条数：排查"东西凭空出现/消失"时要的就是这个。"""
    changes = getattr(decision, "equipment_changes", ())
    return {
        "equipment_changes": [
            f"{c.player or '发言者'}:+{list(c.gained)}-{list(c.lost)}" for c in changes
        ]
    }
