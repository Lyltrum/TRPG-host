"""裁决契约（keeper agent v2 · 两阶段回合制）：LLM 结构化输出的完整词汇表。

「裁决」是守秘人的幕后认知：这个行动要不要检定、状态怎么变。v1 把它做成
agent 的"自由工具调用"，实测被模型的写作本能碾压（该掷不掷/线索白给/状态
不记，三轮 prompt 强化无效）。v2 把裁决抬成**独立 LLM 调用的结构化输出**：
`KeeperDecision` 的字段就是裁决的完整词汇表——检定是 schema 的一部分而不是
"可选的工具"，不存在"忘了裁决"这条路径。

本文件只放 L1 数据契约（schema），不放执行逻辑——"LLM 声明了哪个字段就
调 tools.py 哪个函数"这套编排/调度逻辑在 `turn_executor.py`
（`execute_side_effects`/`create_pending_checks`），2026-07-30 从本文件
拆出去，理由见工程规范整理那次讨论：契约（是什么）和编排（怎么执行）
是两件事，混在一个文件里不容易一眼看出"这是哪一层"。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _DecisionModel(BaseModel):
    # 裁决 JSON 由 LLM 生成，多给的字段忽略、不报错——校验的重点是"必要的
    # 结构在"，不是"一个字都不能多"。
    model_config = ConfigDict(extra="ignore")


class OpposedTarget(_DecisionModel):
    """对抗检定的对手侧（exec/19 #38）。

    真人实测 2026-07-31：裁决器想要「凌铭辉，体质对抗 POT 16」，而 schema 里
    没有任何字段能表达"对手是谁、目标值多少"——它就把这句写进了散文，玩家
    界面上永远不会出现那张掷骰卡片，只能等一个不来的骰子。**schema 表达不了
    的东西会从叙事里漏出去**，这是那条判据的原始案例。

    `value` 是**百分位目标值**（0–100），不是 COC6 的属性点：毒物 POT 16 要
    写成 80（POT×5），NPC 的技能/属性直接用它的百分数。换算由裁决器完成——
    它手里有数据卡，代码手里没有。
    """

    opponent: str = Field(description="对手是谁/什么（NPC 名、毒物、暗流……），展示用")
    value: int = Field(description="对手侧的百分位目标值 0-100（属性点要 ×5）")


class CheckRequest(_DecisionModel):
    """一次技能/属性检定请求。player 为 None = 本轮行动的发起玩家。"""

    skill: str
    player: str | None = None
    reason: str = ""
    opposed: OpposedTarget | None = Field(default=None, description="对抗检定时填；普通检定留 null")


class SanCheckRequest(_DecisionModel):
    player: str | None = None
    loss_on_success: str = "0"
    loss_on_failure: str = "1"
    reason: str = ""


class HpChange(_DecisionModel):
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


class StateUpdate(_DecisionModel):
    key: str
    value: str


class PlayerMove(_DecisionModel):
    """一名调查员**单独**去了别处（分头探索，exec/14 P5.2）。

    跟 `KeeperDecision.current_node_id` 的分工是"默认 vs 覆盖"，不是同一份
    数据的两个角色：`current_node_id` 说的是「本轮发言的人共同到了哪」，
    这里说的是「谁没跟着大家、单独在哪」，后者覆盖前者。全队在一起的常见
    情形下 `moves` 恒为空数组，行为与 P5.2 之前逐字一致。
    """

    player: str = Field(description="调查员昵称或角色名，必须是在场名单里的人")
    node_id: str = Field(description="他单独所在的剧本节点 id，不得编造")


class StealthChange(_DecisionModel):
    """潜行/现身（exec/18 ②）。

    「在场但不可见」：隐匿的调查员照常**听得见**这里发生的一切，但他自己的
    行动不会被同处的其他人看见。被发现、主动现身、离开该地点都要置回 false。
    """

    player: str = Field(description="调查员昵称或角色名")
    hidden: bool = Field(description="true=潜行成功进入隐匿；false=现身/被发现")


class KeeperDecision(_DecisionModel):
    """裁决阶段的完整输出契约。

    所有列表字段默认空——"本轮不需要检定"表现为 `checks=[]` 加上 `thinking`
    里的理由，与 v1 的 declare_no_check 等价但更强：它不是模型"选择调用"的
    工具，而是每轮必然产出的结构化字段，天然可审计（structlog 落盘）。
    """

    # 🔴 必须短。实测（exec/19 #35）裁决那 ~7 秒里瓶颈**不在输入**——system
    # prompt 的模组全文已经被 provider 的 prompt cache 覆盖了（11134 token 命中
    # 10880），耗时几乎全在**输出解码**。而 thinking 是纯审计字段、玩家永远看
    # 不到，模型却常写成 200 字小作文，占掉输出的三分之一。压到 30 字实测
    # completion 370→250 token、单次 7.0s→5.6s。
    thinking: str = Field(default="", description="裁决理由，最多 30 字（审计用，不广播给玩家）")
    checks: list[CheckRequest] = Field(default_factory=list)
    san_checks: list[SanCheckRequest] = Field(default_factory=list)
    hp_changes: list[HpChange] = Field(default_factory=list)
    state_updates: list[StateUpdate] = Field(default_factory=list)
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
    stealth: list[StealthChange] = Field(
        default_factory=list,
        description="潜行状态变化：谁藏起来了 / 谁现身或被发现了（没变化时留空数组）",
    )
    agenda_fired: list[str] = Field(
        default_factory=list, description="本轮真正发生的议程事件 id（不预告）"
    )
    # 路线 5：本轮玩家挣得后可揭开的密级配对 id（须存在于 module.visibility_pairs）
    visibility_revealed: list[str] = Field(
        default_factory=list, description="本轮揭开的 visibility_pair id"
    )
    # 路线 6：开场仪式完成 → investigation；命中结局 → ending 收束
    opening_complete: bool = Field(default=False, description="开场仪式是否已完成（委托已建立等）")
    ending_reached: str | None = Field(default=None, description="本轮命中的结局 id；None=未收束")
    narration_guidance: str = Field(
        default="", description="给叙事阶段的指引：可揭示什么/须保密什么/NPC 如何反应"
    )
    player_state: Literal[
        "confused", "weird_or_meta", "clear_action", "question_to_kp", "normal"
    ] = Field(
        default="normal",
        description="玩家本轮发言的分类：迷茫求指引/怪话或元指令/明确行动/向守秘人问已知信息/都不是",
    )
