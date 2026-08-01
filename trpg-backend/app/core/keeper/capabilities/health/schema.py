"""health 能力贡献给 `KeeperDecision` 的字段片段。"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from app.core.keeper.registry import Capability, DecisionModel


class HpChange(DecisionModel):
    """一笔生命值变化。目标要么是调查员（`player`），要么是 NPC（`npc`）。

    `npc` 是 exec/19 #39 补的口子：战斗里 NPC 当然会掉血，而此前 HP 变更只
    认房间里的玩家，裁决器写「科比特 -4」一律执行失败，NPC 的伤势只活在叙事
    文字里。两个字段分开而不是让 `player` 兼收并蓄——一个字段扮演两个角色
    必出结构性 bug，而且"这个名字指的是玩家还是 NPC"本来就不该靠猜。
    """

    delta: int
    player: str | None = None
    npc: str | None = Field(
        default=None, description="NPC 的 id 或名字，必须取自剧本登场 NPC，与 player 二选一"
    )
    reason: str = ""


class HealthDecisionFields(DecisionModel):
    """被 `KeeperDecision` 继承的字段片段（见 `contract` 那行显式继承）。"""

    hp_changes: list[HpChange] = Field(default_factory=list)


#: 这个能力的字段需要哪种权限。`subject.authorize_decision` 从注册表汇总。
FIELD_CAPABILITIES = {"hp_changes": Capability.ADJUST_HP}


def audit_fields(decision: BaseModel) -> Mapping[str, object]:
    """本轮 HP 变更进 `keeper_decision` 日志的样子。

    只记条数，不记 delta/对象：那些在 `keeper.hp` / `keeper.npc_hp` 两条事件里
    已经逐笔留痕了，这里是"本轮这片能力动没动手"的速览。
    """
    return {"hp_changes": len(getattr(decision, "hp_changes", ()))}
