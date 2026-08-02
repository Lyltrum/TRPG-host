"""world_state 能力贡献给 `KeeperDecision` 的字段片段。"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from app.core.keeper.registry import Capability, DecisionModel


class StateUpdate(DecisionModel):
    """世界状态的一条记账。

    🔴 `subject` 是 exec/24 §8.2 的收口：此前只有自由文本 `key`，模型现编键名
    （实测同一份状态里出现过 `科比特态度`、`包裹状态`、`游戏内时间`）。三个后果：
    同一件事下一轮可能换个名字（"科比特态度" vs "科比特先生态度"）两条并存且
    谁都不报错；键没有主体，**没法按位置/章节裁剪**，长战役里这块会线性膨胀；
    也回答不了"这条状态谁看得见"。

    收口方式是给它一个主体，而不是继续在 key 上打补丁——同 exec/17 的判据：
    **不要用自由文本当标识符**。
    """

    subject: str = Field(
        default="world",
        description=(
            "这条状态挂在谁身上：剧本里的 NPC id / 节点 id；"
            "不属于任何具体实体的世界级状态（游戏内时间、天气、委托进度）填 world。"
            "必须取自剧本，不得编造 id"
        ),
    )
    key: str = Field(description="属性名，如 态度／状态／进度。**不要把主体名字写进 key**")
    value: str


class WorldStateDecisionFields(DecisionModel):
    """被 `KeeperDecision` 继承的字段片段（见 `decision.py` 那行显式继承）。"""

    state_updates: list[StateUpdate] = Field(default_factory=list)


FIELD_CAPABILITIES = {"state_updates": Capability.UPDATE_STATE}


def audit_fields(decision: BaseModel) -> Mapping[str, object]:
    """记键名不记值：值可能很长，而排查时要看的是"这轮记了哪几件事"。"""
    return {"state_updates": [u.key for u in getattr(decision, "state_updates", ())]}
