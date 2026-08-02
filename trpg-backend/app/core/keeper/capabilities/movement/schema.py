"""movement 能力贡献给 `KeeperDecision` 的字段片段。"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, Field

from app.core.keeper.contract.registry import Capability, DecisionModel


class PlayerMove(DecisionModel):
    """一名调查员的位置**单独**被指定（分头探索，exec/14 P5.2）。

    跟 `KeeperDecision.current_node_id` 的分工是"默认 vs 覆盖"，不是同一份
    数据的两个角色：`current_node_id` 说的是「本轮发言的人共同到了哪」，
    这里说的是「谁的位置不由那个默认值决定」，后者覆盖前者。全队在一起的常见
    情形下 `moves` 恒为空数组，行为与 P5.2 之前逐字一致。

    🔴 exec/25 #60：它有**两个**用途，不只是"分头"——
    1. 有人单独去了别处（原本的分头探索）；
    2. **有人被真人带上了**。AI 队友不再自己宣告行动（它只在讨论区出主意），
       所以真人说「我和阿铁一起去地下室」时，阿铁不在"本轮发言的人"里、
       不会被 `current_node_id` 带走，必须在这里显式写一条，否则他被留在原地。

    两个用途写法完全一样，区别只在语义。docstring 第一版只写了用途 1，
    于是模型不会想到用它表达用途 2——**能表达但描述挡住了**，同
    「schema 表达不了的东西会从叙事里漏出去」是一族。
    """

    player: str = Field(
        description="调查员昵称或角色名，必须是在场名单里的人（含被真人带上的 AI 队友）"
    )
    node_id: str = Field(description="他单独所在的剧本节点 id，不得编造")


class HidingChange(DecisionModel):
    """潜行/现身（exec/18 ②）。

    「在场但不可见」：隐匿的调查员照常**听得见**这里发生的一切，但他自己的
    行动不会被同处的其他人看见。被发现、主动现身、离开该地点都要置回 false。

    类名与字段名从 `stealth` 改成 `hiding`（exec/27 三处撞名）：`stealth` 同时
    是 COC7 规则表里的**技能 id**（掷潜行检定用），而**技能 id 不能改**——那是
    规则权威。两个都叫 stealth 的时候，`checks: [{"skill_id": "stealth"}]` 和
    `stealth: [{"hidden": true}]` 读起来像同一件事，其实一个是能力值一个是状态。
    """

    player: str = Field(description="调查员昵称或角色名")
    hidden: bool = Field(description="true=潜行成功进入隐匿；false=现身/被发现")


class MovementDecisionFields(DecisionModel):
    """被 `KeeperDecision` 继承的字段片段（见 `decision.py` 那行显式继承）。"""

    # 场景指针结构化（04 遗留项）：state_updates 里的「当前场景」是人类可读
    # 地名，这个字段是同一件事的机器可读版——剧本节点树里真实存在的 id
    # （见 module_loader.render_full 每个节点标题旁的 `id: xxx`）。取代此前
    # check_guard 对自由文本地名做的模糊字符串匹配。
    #
    # exec/14 P5.2：语义收窄为「本轮**发言的**调查员共同到了哪」——不再是
    # "全房间都在这"。没发言的人位置不动（分头探索时他还在别处，不能被这
    # 一个字段隔空传送走）。谁单独在别处走 `moves`。
    current_node_id: str | None = Field(
        default=None,
        description=(
            "本轮结束时**发言的调查员共同**所在的剧本节点 id；无法对应到已知节点或场景"
            "未变化时留空，不要编造不存在的 id"
        ),
    )
    moves: list[PlayerMove] = Field(
        default_factory=list,
        description="分头探索：谁单独去了别处（全队在一起时留空数组）",
    )
    hiding: list[HidingChange] = Field(
        default_factory=list,
        description="潜行状态变化：谁藏起来了 / 谁现身或被发现了（没变化时留空数组）",
    )


FIELD_CAPABILITIES = {
    # 分头探索（P5.2）：逐人位置与设置场景是同一件事的不同粒度，共用一条。
    "current_node_id": Capability.SET_SCENE,
    "moves": Capability.SET_SCENE,
    # 潜行**单独一条**：它是已经成立的状态，不该跟着"世界不推进"一起被收走
    # （exec/27 阶段 3 · B 族，`turn_policy` 模块说明里有完整理由）。
    "hiding": Capability.SET_HIDING,
}


def audit_fields(decision: BaseModel) -> Mapping[str, object]:
    """位置是多人局里一切的地基，逐人记谁去了哪。"""
    return {"moves": [f"{m.player}→{m.node_id}" for m in getattr(decision, "moves", ())]}
