"""能力注册表的**机制**（exec/27 阶段 2）——本文件不认识任何一个具体能力。

## 它在解决什么

一个守秘人能力（"HP 变化"、"技能检定"、"分头移动"）天然横跨好几层：schema
教模型能说什么、prompt 教模型什么时候说、executor 把它变成世界的改变、
situation 把结果再喂回模型眼前、audit 让它在日志里留得下痕。此前这几段分别
躺在 `decision.py` / `prompts.py` / `tools.py` / `agent.py` 里——实测一个功能
平均改 4.1 个文件，`agent.py` 被 90 个功能里的 46 个碰过。**那不是文件放错了
地方，是能力被按技术层切碎了。**

本模块定义那五个钩子的形状。具体能力在 `capabilities/<名字>/` 里各自组装一个
`KeeperCapability` 交上来，骨架（decision/prompts/turn_executor/agent）只跟
这份契约打交道。

## 🔴 为什么这里是叶子

`capabilities/*` 要 import 它（取 `Capability` 枚举、`DecisionModel` 基类），
所以它**一个 `app.*` 都不能 import**——否则 `contract → capabilities → contract`
当场成环。`ScenarioModule`/`KeeperDeps` 只在 `TYPE_CHECKING` 下引入，运行时
不产生依赖边。阶段 0 的架构测试盯着这条。

## 顺序为什么必须显式

`order` 一律是显式数字，不靠字典序、不靠 import 顺序（`exec/27` 障碍 1/2）：

- schema 片段合并后字段顺序要稳定，否则同一份裁决 schema 会抖；
- 裁决 prompt 的规则是**带编号的**（规则 1–12），块的先后有语义；
- 执行顺序有语义（`moves` 必须排在 `current_node_id` 之后，否则被默认值盖掉）；
- 局面块的先后决定模型先看到什么。

留出的间隔（10/20/30…）是给后续能力插队用的，插进去不必重排别人。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注，运行时不产生依赖边
    from app.core.keeper.deps import KeeperDeps
    from app.core.keeper.module_loader import ScenarioModule


class DecisionModel(BaseModel):
    """裁决 schema 的公共基类。

    裁决 JSON 由 LLM 生成，多给的字段忽略、不报错——校验的重点是"必要的结构
    在"，不是"一个字都不能多"。

    放在这里而不是 `decision.py`，是因为各能力的字段片段都要继承它，而
    `decision.py` 反过来要 import 那些片段。
    """

    model_config = ConfigDict(extra="ignore")


class Capability(StrEnum):
    """一个主体**有权做**的事。与 `KeeperDecision` 的动作字段一一对应。

    从 `subject.py` 挪到这里：能力片段（`capabilities/*/schema.py`）要声明
    "我这个字段需要哪种权限"，而 `subject.py` 是 `decision.py` 的下游，
    留在那儿会成环。
    """

    REQUEST_CHECK = "request_check"
    REQUEST_SAN_CHECK = "request_san_check"
    ADJUST_HP = "adjust_hp"
    UPDATE_STATE = "update_state"
    SET_SCENE = "set_scene"
    #: 「此刻藏着没」。与 SET_SCENE 分开而不是共用一条：玩家向守秘人提问那一轮
    #: 要收走移动与场景指针，但**不该让藏起来的人现身**（exec/27 阶段 3 · B 族）。
    SET_HIDING = "set_hiding"
    FIRE_AGENDA = "fire_agenda"
    REVEAL_VISIBILITY = "reveal_visibility"
    ADVANCE_PHASE = "advance_phase"


#: 裁决 prompt 里的插槽。规则清单与输出格式示例是两处独立的有序文本，
#: 一个能力通常两处都要贡献一句。
PromptSlot = Literal["rules", "output_example"]


@dataclass(frozen=True)
class PromptBlock:
    """裁决 prompt 里的一段文本。

    `text` 是**成品原文**（含"3b."这样的编号），骨架只负责按 order 排好后
    拼起来——编号语义属于能力自己，骨架不该替它编号。
    """

    slot: PromptSlot
    order: float
    text: str


@dataclass(frozen=True)
class SituationBlock:
    """往「局面块」贡献一段模型每轮都看得见的状态。

    🔴 这个钩子是切 health 试点时**在真代码里发现漏掉的**（`agent.py` 那行
    `format_npc_states`）：能力不只要能改世界，还得让模型**看见**自己改成了
    什么样，否则下一轮裁决只能从上一段散文里猜（`exec/19 #39` 的原始症状）。

    `render` 返回空串 = 本轮没有内容，整块连标题一起不渲染。
    """

    order: float
    heading: str
    render: Callable[[ScenarioModule, dict | None], str]


#: 审计钩子：从本轮裁决里挑出该进 `keeper_decision` 结构化日志的字段。
#:
#: 🔴 这是第五个钩子，跟 `situation` 一样是**切到一半在真代码里发现漏掉的**。
#: 没有它，`agent.py` 那行 `logger.info("keeper_decision", ...)` 就得逐个列出
#: 每个能力的字段——于是"加一个能力不改编排层一行"当场不成立，而且漏了不会
#: 报错，只是那片能力从此在日志里**隐身**：线上排查时看不出它本轮做没做事。
AuditFn = Callable[[BaseModel], Mapping[str, object]]


#: 执行钩子：拿到本轮裁决，做完自己那部分副作用，返回 (执行报告, 问题清单)。
#: 报告喂给叙事阶段（叙事必须知道"发生了什么"），问题清单是"裁决里不合法的项"
#: ——跳过不炸，一并交给叙事自然圆场。
ExecutorFn = Callable[["KeeperDeps", BaseModel], Awaitable[tuple[list[str], list[str]]]]


@dataclass(frozen=True)
class ExecutorHook:
    order: float
    run: ExecutorFn


@dataclass(frozen=True)
class KeeperCapability:
    """一个能力交给系统的全部东西。

    `schema` 是这个能力贡献给 `KeeperDecision` 的字段片段（一个 mixin 模型）。
    ⚠️ 它**不会**被自动挂上去——`decision.py` 里那行显式继承才是权威，这里
    留一份是为了让 `tests/test_capability_registry.py` 能反过来验证"注册了
    却忘了继承"。动态拼基类可以省掉那一行，但会让静态类型检查彻底看不见
    `decision.hp_changes`，代价不划算。
    """

    name: str
    schema: type[BaseModel] | None = None
    #: 决策字段 → 动这个字段需要的权限（`subject.authorize_decision` 用）。
    field_capabilities: Mapping[str, Capability] = field(default_factory=dict)
    prompt_blocks: Sequence[PromptBlock] = ()
    executors: Sequence[ExecutorHook] = ()
    situations: Sequence[SituationBlock] = ()
    #: 往 `keeper_decision` 日志与 `keeper.decision` 事件贡献的字段
    #:（None = 这个能力没什么好审计的）。
    audit: AuditFn | None = None
    #: 这个能力在 `keeper_state` 里占的键。声明出来同时管两件事：
    #: **`state_updates` 不许写**，且**不原样喂给模型**（模型看到的是 situation
    #: 钩子渲染好的那一块）。
    #:
    #: 🔴 第六个钩子，切 `agenda` 时暴露的。原本 `tools._RESERVED_STATE_KEYS`
    #: 与 `agent._hidden_keys` 是**两张各自手维护的清单**，实测已经分叉：
    #: `NPC状态` 两张都没进，于是模型一条 `state_updates` 就能把 NPC 血量记录
    #: 覆盖成一个字符串、`load_npc_states` 静默返回 {}。所以这里只有一张清单，
    #: 「代码记账的键不原样喂给模型」由代码保证而不是靠两处同步。
    reserved_state_keys: Sequence[str] = ()
