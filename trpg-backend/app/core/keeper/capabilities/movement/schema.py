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


class NewLocation(DecisionModel):
    """申请一个剧本里没有的地点（exec/32）。

    🔴 **新建必须是一个结构上不同的动作**，不能让模型直接往 `current_node_id`
    里写一个没见过的字符串"顺便新建"——那正是 `exec/31 #72` 的形状：同一个字段
    同时表达"去已知的地方"和"去新地方"，就分不清**写错了**和**想新建**，
    于是错的那一半只能靠白名单硬拦（真机三次全中，它写的是个 NPC id）。

    id 由代码分配，这里只给名字。名字**不是标识符**，重名不去重。
    """

    name: str = Field(description="这个地方叫什么，玩家听得懂的短名（如「卡比家」）")
    from_id: str | None = Field(
        default=None,
        description="从哪个已知地点去的（剧本节点 id 或 loc-N），不确定就留空",
    )


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
    #
    # exec/32：取值域从"剧本节点 id"扩成"剧本节点 id ∪ 本局的即兴地点 loc-N"。
    # 两类都在局面块里列全，模型只能挑；要去清单上没有的地方走 `new_location`。
    current_node_id: str | None = Field(
        default=None,
        description=(
            "本轮结束时**发言的调查员共同**所在的地点 id（剧本节点 id 或局面块里列出的"
            " loc-N）；场景未变化时留空，不要编造不存在的 id——要去清单上没有的地方"
            "请用 new_location"
        ),
    )
    new_location: NewLocation | None = Field(
        default=None,
        description=(
            "玩家去了剧本和已知清单里都没有的地方时填它（如原文提过但没写成场景的"
            "「卡比家」）；系统会分配 id 并把发言的人挪过去。没有新地方时填 null"
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
    # 建一个新地点也是"定位这件事"的一部分：不许改场景的主体也不该能建地点。
    "new_location": Capability.SET_SCENE,
    # 潜行**单独一条**：它是已经成立的状态，不该跟着"世界不推进"一起被收走
    # （exec/27 阶段 3 · B 族，`turn_policy` 模块说明里有完整理由）。
    "hiding": Capability.SET_HIDING,
}


def audit_fields(decision: BaseModel) -> Mapping[str, object]:
    """位置是多人局里一切的地基，逐人记谁去了哪。"""
    return {"moves": [f"{m.player}→{m.node_id}" for m in getattr(decision, "moves", ())]}
