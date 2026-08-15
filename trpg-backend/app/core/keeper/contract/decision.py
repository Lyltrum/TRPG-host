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
from app.core.keeper.capabilities.cast.schema import CastDecisionFields
from app.core.keeper.capabilities.closure.schema import ClosureDecisionFields
from app.core.keeper.capabilities.clue_reveal.schema import ClueRevealDecisionFields
from app.core.keeper.capabilities.health.schema import HealthDecisionFields
from app.core.keeper.capabilities.madness.schema import MadnessDecisionFields
from app.core.keeper.capabilities.movement.schema import MovementDecisionFields
from app.core.keeper.capabilities.open_threads.schema import OpenThreadsDecisionFields
from app.core.keeper.capabilities.progression.schema import ProgressionDecisionFields
from app.core.keeper.capabilities.san_check.schema import SanCheckDecisionFields
from app.core.keeper.capabilities.skill_check.schema import SkillCheckDecisionFields
from app.core.keeper.capabilities.world_state.schema import WorldStateDecisionFields
from app.core.keeper.contract.registry import DecisionModel


class KeeperDecision(
    AgendaDecisionFields,
    CastDecisionFields,
    ClosureDecisionFields,
    ClueRevealDecisionFields,
    HealthDecisionFields,
    MadnessDecisionFields,
    MovementDecisionFields,
    OpenThreadsDecisionFields,
    ProgressionDecisionFields,
    SanCheckDecisionFields,
    SkillCheckDecisionFields,
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
        "wrap_up",
        "normal",
    ] = Field(
        default="normal",
        description=(
            "玩家本轮发言的分类：迷茫求指引/怪话或元指令/明确行动/向守秘人问已知信息/"
            "征询可行性或许可/对他人动手或强行突破/出戏地想收场/都不是"
        ),
    )
