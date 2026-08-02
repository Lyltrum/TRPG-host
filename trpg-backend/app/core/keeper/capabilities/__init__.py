"""能力清单 + 五个钩子的汇总函数（exec/27 阶段 2）。

**加一个能力 = 新建一个目录 + 在下面这个元组里加一行。**（唯一的例外是
schema 片段还要在 `decision.py` 里显式继承一次，理由见 `registry` 里
`KeeperCapability.schema` 的说明；漏了会被 `test_capability_registry.py`
当场抓住，不会静默。）

🔴 这个 `__init__` 是**唯一**允许认识所有能力的地方。各能力之间互不 import
（架构测试盯着），共用的东西要么下沉到 `registry`/`deps`，要么就说明边界
切错了——`exec/27` 里 `checks` 被拆成 `skill_check`/`san_check`/`primitives`
就是这条判据抓出来的。
"""

from collections.abc import Sequence

from pydantic import BaseModel

from app.core.keeper.capabilities.agenda import CAPABILITY as AGENDA
from app.core.keeper.capabilities.clue_reveal import CAPABILITY as CLUE_REVEAL
from app.core.keeper.capabilities.health import CAPABILITY as HEALTH
from app.core.keeper.capabilities.progression import CAPABILITY as PROGRESSION
from app.core.keeper.module_loader import ScenarioModule
from app.core.keeper.registry import (
    Capability,
    ExecutorHook,
    KeeperCapability,
    PromptBlock,
    PromptSlot,
    SituationContext,
)

#: 已经垂直切出来的能力。其余的还散在骨架里，逐个切（exec/27 阶段 3）。
CAPABILITIES: tuple[KeeperCapability, ...] = (HEALTH, AGENDA, PROGRESSION, CLUE_REVEAL)


def field_capabilities() -> dict[str, Capability]:
    """汇总「决策字段 → 需要的权限」。"""
    merged: dict[str, Capability] = {}
    for capability in CAPABILITIES:
        merged.update(capability.field_capabilities)
    return merged


def prompt_blocks(slot: PromptSlot) -> list[PromptBlock]:
    """某个插槽下的全部文本块，按 order 升序。"""
    blocks = [b for c in CAPABILITIES for b in c.prompt_blocks if b.slot == slot]
    return sorted(blocks, key=lambda b: b.order)


def executors() -> list[ExecutorHook]:
    """全部执行钩子，按 order 升序。骨架把它们并进自己那串副作用里。"""
    return sorted((h for c in CAPABILITIES for h in c.executors), key=lambda h: h.order)


def situation_blocks(
    module: ScenarioModule, keeper_state: dict | None, *, observer_id: str | None = None
) -> list[tuple[float, str]]:
    """渲染各能力要摆在模型眼前的状态，返回 (order, 成品文本块)。

    `render` 返回空串 = 本轮没有内容，整块连标题一起不渲染——没记过账的对局
    局面块与切分前逐字一致。
    """
    context = SituationContext(module=module, keeper_state=keeper_state, observer_id=observer_id)
    rendered: list[tuple[float, str]] = []
    for capability in CAPABILITIES:
        for block in capability.situations:
            body = block.render(context)
            if body:
                rendered.append((block.order, f"## {block.heading}\n{body}\n\n"))
    return sorted(rendered, key=lambda item: item[0])


def audit_fields(decision: BaseModel) -> dict[str, object]:
    """各能力要留痕的字段，合并成一份日志 kwargs。

    键冲突会当场炸——两个能力抢同一个日志字段名，日志里只会剩一个，而且是
    静默的。宁可在启动/第一轮就失败。
    """
    merged: dict[str, object] = {}
    for capability in CAPABILITIES:
        if capability.audit is None:
            continue
        for key, value in capability.audit(decision).items():
            if key in merged:
                raise ValueError(f"能力 {capability.name!r} 的审计字段 {key!r} 与别的能力撞名")
            merged[key] = value
    return merged


def reserved_state_keys() -> frozenset[str]:
    """各能力占用的 `keeper_state` 键：`state_updates` 不许写、也不原样喂给模型。"""
    return frozenset(k for c in CAPABILITIES for k in c.reserved_state_keys)


def registered_schemas() -> Sequence[type[BaseModel]]:
    """各能力贡献的 schema 片段（`test_capability_registry.py` 用来验证
    `KeeperDecision` 真的把它们都继承了）。"""
    return tuple(c.schema for c in CAPABILITIES if c.schema is not None)
