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

## 🔴 这份 schema 是**组装**出来的（exec/27 阶段 2）

LLM 只能收一份整体 schema，切不了片；但"哪些字段属于哪个能力"该由能力自己
说了算。做法是每个能力提供一个字段片段（pydantic mixin），`KeeperDecision`
显式继承它们——**继承顺序就是字段顺序**，稳定、可读、静态类型看得见。

为什么不用 `create_model` 动态拼基类（省掉下面那行显式继承）：那样
`decision.hp_changes` 在类型检查器眼里就不存在了，整条链的类型安全为一行
样板买单，不划算。漏继承的风险由 `test_capability_registry.py` 兜住——
注册了却没继承会当场变红，不会静默。
"""

from typing import Literal

from pydantic import Field

from app.core.keeper.capabilities.agenda.schema import AgendaDecisionFields
from app.core.keeper.capabilities.clue_reveal.schema import ClueRevealDecisionFields
from app.core.keeper.capabilities.health.schema import HealthDecisionFields
from app.core.keeper.capabilities.movement.schema import MovementDecisionFields
from app.core.keeper.capabilities.progression.schema import ProgressionDecisionFields
from app.core.keeper.capabilities.world_state.schema import WorldStateDecisionFields
from app.core.keeper.registry import DecisionModel


class OpposedTarget(DecisionModel):
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


class CheckRequest(DecisionModel):
    """一次技能/属性检定请求。player 为 None = 本轮行动的发起玩家。

    🔴 `skill_id` 是**白名单 id**，不是中文名（exec/17）：技能用规则表 id
    （`spot-hidden`、`fighting-brawl`），属性用属性 key（`STR`、`CON`）。

    为什么不是自由文本：中文名两侧都是人/模型随手写的（裁决器按自己的 COC7
    常识说"侦查"，规则表叫"侦察"），指望逐字相同不现实，而事后维护同义词
    字典是打地鼠——换个模组、模型换个措辞就又漏一个。id 是封闭集合（92+9），
    随规则版本变、不随模组变。**tool calling 不解决这个问题**（它的参数同样
    是模型写的字符串），解决它的是 enum/白名单。

    ⚠️ 如实记：DeepSeek 走的是 JSON mode 而不是带 schema 的 structured output，
    所以 enum **约束不到生成**，只能靠 prompt 给表 + 代码校验。模型仍写中文名
    时代码会**显式回退**并打 `keeper_skill_id_fallback` warning——不是静默，
    日志能统计守规率，据此再决定要不要收紧成硬失败。
    """

    skill_id: str = Field(description="技能 id 或属性 key，必须取自权威 id 表")
    player: str | None = None
    reason: str = ""
    opposed: OpposedTarget | None = Field(default=None, description="对抗检定时填；普通检定留 null")


class SanCheckRequest(DecisionModel):
    player: str | None = None
    loss_on_success: str = "0"
    loss_on_failure: str = "1"
    reason: str = ""


class KeeperDecision(
    AgendaDecisionFields,
    ClueRevealDecisionFields,
    HealthDecisionFields,
    MovementDecisionFields,
    ProgressionDecisionFields,
    WorldStateDecisionFields,
    DecisionModel,
):
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
    narration_guidance: str = Field(
        default="", description="给叙事阶段的指引：可揭示什么/须保密什么/NPC 如何反应"
    )
    player_state: Literal[
        "confused",
        "weird_or_meta",
        "clear_action",
        "question_to_kp",
        "feasibility_question",
        "physical_conflict",
        "normal",
    ] = Field(
        default="normal",
        description=(
            "玩家本轮发言的分类：迷茫求指引/怪话或元指令/明确行动/向守秘人问已知信息/"
            "征询可行性或许可/对他人动手或强行突破/都不是"
        ),
    )
