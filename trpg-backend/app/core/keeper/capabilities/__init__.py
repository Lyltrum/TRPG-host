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
from app.core.keeper.capabilities.closure import CAPABILITY as CLOSURE
from app.core.keeper.capabilities.clue_reveal import CAPABILITY as CLUE_REVEAL
from app.core.keeper.capabilities.health import CAPABILITY as HEALTH
from app.core.keeper.capabilities.luck_spend import CAPABILITY as LUCK_SPEND
from app.core.keeper.capabilities.movement import CAPABILITY as MOVEMENT
from app.core.keeper.capabilities.progression import CAPABILITY as PROGRESSION
from app.core.keeper.capabilities.san_check import CAPABILITY as SAN_CHECK
from app.core.keeper.capabilities.skill_check import CAPABILITY as SKILL_CHECK
from app.core.keeper.capabilities.world_state import CAPABILITY as WORLD_STATE
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.contract.registry import (
    Capability,
    ExecutorHook,
    KeeperCapability,
    PendingHook,
    PostSettleHook,
    PromptBlock,
    PromptSlot,
    SettleHook,
    SituationContext,
)

#: 已经垂直切出来的能力。其余的还散在骨架里，逐个切（exec/27 阶段 3）。
CAPABILITIES: tuple[KeeperCapability, ...] = (
    HEALTH,
    AGENDA,
    PROGRESSION,
    CLOSURE,
    CLUE_REVEAL,
    WORLD_STATE,
    MOVEMENT,
    LUCK_SPEND,
    SKILL_CHECK,
    SAN_CHECK,
)


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


def pendings() -> list[PendingHook]:
    """全部待掷钩子，按 order 升序。"""
    return sorted((h for c in CAPABILITIES for h in c.pendings), key=lambda h: h.order)


def settle_hook_for(kind: str) -> SettleHook:
    """认领这种待掷记录的结算钩子（掷骰 + 生效两半）。没人认领就炸——
    **不要有 else 兜底**。

    🔴 兜底就是静默走错分支：加一种新检定时，"发起"会自动接上（`pending` 钩子
    遍历全部能力），而结算若有 else，那条新检定会被当成别的类型结算掉，掷骰
    数字照样出现在玩家屏幕上，没有任何东西会红。

    返回整个钩子而不是单独的 `run`：掷骰与生效是同一件事的两头，拿走一半的
    调用方迟早会忘了另一半（`exec/34` 第 3 步）。
    """
    for capability in CAPABILITIES:
        for hook in capability.settlers:
            if hook.kind == kind:
                return hook
    raise KeyError(f"没有能力认领 kind={kind!r} 的待掷检定结算")


def post_settles() -> list[PostSettleHook]:
    """全部「结算之后再等玩家一拍」的钩子，按 order 升序。"""
    return sorted((h for c in CAPABILITIES for h in c.post_settles), key=lambda h: h.order)


def post_settle_for(kind: str) -> PostSettleHook:
    """认领这种「等玩家一拍」的钩子。没人认领就炸，理由同 `settle_hook_for`。"""
    for capability in CAPABILITIES:
        for hook in capability.post_settles:
            if hook.kind == kind:
                return hook
    raise KeyError(f"没有能力认领 kind={kind!r} 的待决定项")


def executors() -> list[ExecutorHook]:
    """全部执行钩子，按 order 升序。骨架把它们并进自己那串副作用里。"""
    return sorted((h for c in CAPABILITIES for h in c.executors), key=lambda h: h.order)


def situation_blocks(
    module: ScenarioModule,
    keeper_state: dict | None,
    *,
    observer_id: str | None = None,
    players: tuple[tuple[str, str], ...] = (),
    merge_pending: frozenset[str] = frozenset(),
) -> list[tuple[float, str]]:
    """渲染各能力要摆在模型眼前的状态，返回 (order, 成品文本块)。

    `render` 返回空串 = 本轮没有内容，整块连标题一起不渲染——没记过账的对局
    局面块与切分前逐字一致。
    """
    context = SituationContext(
        module=module,
        keeper_state=keeper_state,
        observer_id=observer_id,
        players=players,
        merge_pending=merge_pending,
    )
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
    """各能力占用的 `keeper_state` 键：`state_updates` 不许写、也不原样喂给模型。

    🔴 **唯一来源。** 编排层把它塞进 `KeeperDeps` 带给执行层——能力自己 import
    这个汇总模块会成环（`capabilities → 某能力 → 汇总 → capabilities`）。
    """
    return frozenset(k for c in CAPABILITIES for k in c.reserved_state_keys) | _SKELETON_KEYS


#: 还没切出去的能力占的键。**空了**——七片切完后所有保留键都由能力自己声明。
_SKELETON_KEYS: frozenset[str] = frozenset()


def visible_keeper_state(keeper_state: dict | None) -> dict | None:
    """喂给模型的那份世界状态笔记：滤掉所有代码记账的键。

    🔴 与 `reserved_state_keys()`（`state_updates` 不许写）**共用同一个集合**，
    不是两张各自维护的清单。此前是两张，实测已经分叉：`NPC状态` 两张都漏了，
    于是模型既看得见那个 dict 的原始形态，又能用一条 `state_updates` 把它覆盖
    成字符串、让血量记账静默清零（exec/27 阶段 3 复查复现）。

    这些键要么是机器格式（逐人位置是 `player_id@node_id`、隐匿玩家是 player id），
    要么已经由 situation 钩子渲染成人话摆在局面块里。空字典/None 原样返回。
    """
    if not keeper_state:
        return keeper_state
    reserved = reserved_state_keys()
    return {k: v for k, v in keeper_state.items() if k not in reserved}


def registered_schemas() -> Sequence[type[BaseModel]]:
    """各能力贡献的 schema 片段（`test_capability_registry.py` 用来验证
    `KeeperDecision` 真的把它们都继承了）。"""
    return tuple(c.schema for c in CAPABILITIES if c.schema is not None)
