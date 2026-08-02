"""主体与权限（exec/14 P2）—— `view(subject)` 抽象的落地。

## 这一层在解决什么

在此之前，「守秘人有权改 HP、玩家没有」这件事**不存在于代码里**——它隐含在
"只有一条代码路径会调 `adjust_hp_impl`"这个事实里。想加 NPC 主体时，它会被
写成"另一条也直接调 `*_impl` 的代码路径"，而没有任何东西能阻止它读剧本
（读剧本就是 `render_full(module)` 一个函数调用，不是一次受权限管辖的读取）。

本模块把这件事显式化：**主持人 / NPC / 玩家是同一种东西，只是视图与权限不同。**

## 🔴 权限的载体是"调用前给它的 schema"，不是"调用形式"

一次 tool call 在网络上就是 `{name, arguments}` 的 JSON，跟结构化输出没有本质
区别。**权限不在调用形式里，在你调用前给模型看什么**——tool calling 是"放了
哪些 tool 定义"，结构化输出是"给了哪个 schema"。两者同构，而受限 schema **更强**：
越权动作**无法表达**（字段不存在），不是"能表达但被拦住"。

所以做权限不需要回到 agent 形状。控制流保持 workflow：Engine 决定何时问哪个
主体，主体只在被问到时用自己的视图做**一次**决策。主体化 ≠ 自主化。

## 纵深防御

`build_decision_model` 让越权动作无法表达，`authorize_decision` 在执行边界再查
一遍。第二道不是冗余：老 schema 反序列化出来的决策、测试构造的决策、以后从
别处传进来的决策，都可能绕过第一道。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, create_model

from app.core.keeper.capabilities import field_capabilities
from app.core.keeper.decision import KeeperDecision
from app.core.keeper.registry import Capability, DecisionModel

#: 裁决字段 → 需要的能力。不在这张表里的字段（thinking / narration_guidance /
#: player_state）不是"动作"，任何主体都可以产出。
#:
#: 🔴 已经垂直切出去的能力（exec/27）自带这一行，从注册表汇总进来——它们的
#: 权限声明跟 schema 躺在同一个目录里，不必回这里改一次。下面手写的那些是
#: 还没切的能力，逐个切走（阶段 3）。
DECISION_FIELD_CAPABILITIES: dict[str, Capability] = {
    "checks": Capability.REQUEST_CHECK,
    "san_checks": Capability.REQUEST_SAN_CHECK,
    "current_node_id": Capability.SET_SCENE,
    # 分头探索（P5.2）：逐人位置与设置场景是同一件事的不同粒度，共用一条。
    "moves": Capability.SET_SCENE,
    # 潜行**单独一条**：它是已经成立的状态，不该跟着"世界不推进"一起被收走
    # （exec/27 阶段 3 · B 族，`turn_policy` 模块说明里有完整理由）。
    "stealth": Capability.SET_HIDING,
    **field_capabilities(),
}

ALL_CAPABILITIES = frozenset(Capability)


@dataclass(frozen=True)
class Subject:
    """一个有视图、有权限的参与者。

    `known_fact_ids=None` 表示**全集**（守秘人）；其余主体持有一个显式的
    已知事实集合，`view()` 据此过滤。
    """

    id: str
    kind: Literal["keeper", "npc"]
    capabilities: frozenset[Capability] = frozenset()
    known_fact_ids: frozenset[str] | None = None

    def can(self, capability: Capability) -> bool:
        return capability in self.capabilities

    @property
    def sees_meta(self) -> bool:
        """能不能看见元层（结局 / 议程 / 主持指导 / 真相层）。

        只有守秘人可以。元层对**任何虚构内主体**永不可见——这是可见性的
        「层次轴」，与「时序轴」（未发现 ↔ 已挣得）正交。
        """
        return self.kind == "keeper"

    def knows(self, fact_id: str) -> bool:
        return self.known_fact_ids is None or fact_id in self.known_fact_ids


#: 守秘人：全视图、全权限。它是"视图取全集的那个主体"，不是特殊物种。
KEEPER = Subject(id="keeper", kind="keeper", capabilities=ALL_CAPABILITIES, known_fact_ids=None)


def npc_subject(npc_id: str, known_fact_ids: frozenset[str]) -> Subject:
    """一个 NPC 主体。

    ⚠️ 本期 `capabilities` 为空——NPC 还不能改世界状态（那是 P6）。它现在的
    唯一意义是**视图受限**：知道的事实由 `module.npcs[].knows` 决定，看不见元层。

    注意 NPC 在虚构内知道的常常**超过**玩家（教徒知道教团、图书管理员知道
    书库布局）；它缺的不是信息量，是元层。
    """
    return Subject(id=npc_id, kind="npc", capabilities=frozenset(), known_fact_ids=known_fact_ids)


def authorize_decision(subject: Subject, decision: KeeperDecision) -> list[str]:
    """返回越权项清单（空 = 全部合法）。不抛异常——由调用方决定怎么处理。

    只检查"有值"的字段：默认值（空列表 / None / False）不构成一次动作。
    """
    violations: list[str] = []
    for field, capability in DECISION_FIELD_CAPABILITIES.items():
        value = getattr(decision, field, None)
        if not value:  # [] / None / False / "" 都不算动作
            continue
        if not subject.can(capability):
            violations.append(
                f"主体 {subject.id!r}（{subject.kind}）无权 {capability.value}：{field}"
            )
    return violations


def sanitize_decision(subject: Subject, decision: KeeperDecision) -> KeeperDecision:
    """把越权字段清回默认值，返回可安全执行的决策。

    全部合法时**原样返回同一个对象**（不是等价副本）——守秘人这条路径上
    连一次拷贝都不发生，是 P2「重构不改行为」的一部分。
    """
    violations = {
        field
        for field, capability in DECISION_FIELD_CAPABILITIES.items()
        if getattr(decision, field, None) and not subject.can(capability)
    }
    if not violations:
        return decision
    defaults = {
        field: KeeperDecision.model_fields[field].get_default(call_default_factory=True)
        for field in violations
    }
    return decision.model_copy(update=defaults)


def build_decision_model(capabilities: frozenset[Capability]) -> type[BaseModel]:
    """按能力集构造裁决 schema —— 越权动作在这里就**无法表达**。

    全权限时直接返回 `KeeperDecision` 本身（不是等价副本）：守秘人这条路径
    上一个字节都不变，是 P2「重构不改行为」这条硬要求的一部分。

    返回类型标 `type[BaseModel]` 而不是 `type[KeeperDecision]`——受限模型
    **确实不是** KeeperDecision（字段更少），标成它就是对类型系统撒谎，
    调用方会以为能安全读被禁字段。
    """
    if capabilities >= ALL_CAPABILITIES:
        return KeeperDecision

    fields: dict[str, Any] = {}
    for name, info in KeeperDecision.model_fields.items():
        required = DECISION_FIELD_CAPABILITIES.get(name)
        if required is not None and required not in capabilities:
            continue
        fields[name] = (info.annotation, info)
    name = f"Decision_{'_'.join(sorted(c.value for c in capabilities)) or 'readonly'}"
    # 基类取 DecisionModel（而不是 KeeperDecision）才能真正"少字段"——继承
    # KeeperDecision 会把被禁的字段一起带过来。extra='ignore' 也从它继承。
    return create_model(name, __base__=DecisionModel, **fields)
