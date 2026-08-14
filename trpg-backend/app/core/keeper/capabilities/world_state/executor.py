"""world_state 能力：裁决器记的自由文本世界状态。

`keeper_state` 本身是**共享存储**，各能力都在里面占键；这里管的是"裁决器可以
记一条自己想记的状态"这件事。写入闸门（哪些键由代码记账、不许它碰）来自
`deps.reserved_state_keys`——那是所有能力声明的并集，由编排层带下来，理由见
`deps.py` 里那个字段的说明。
"""

from __future__ import annotations

import structlog
from pydantic import BaseModel

from app.core.keeper.capabilities.world_state.game_time import GAME_TIME_KEY, goes_backwards
from app.core.keeper.contract.module_loader import ScenarioModule, iter_all_nodes
from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.primitives.npcs import resolve_npc_id
from app.core.keeper.runtime.deps import KeeperDeps, KeeperToolError, record_event
from app.core.keeper.runtime.scene_state import SCENE_NAME_KEY
from app.models.room import Room

logger = structlog.get_logger()


WORLD_SUBJECT = "world"


def resolve_state_subject(module: ScenarioModule, label: str) -> str | None:
    """把裁决器写的主体解析成剧本里的 id。解析不出返回 None。

    接受：`world`、NPC id/名字（复用 `resolve_npc_id`，含形态）、节点 id/标题。
    **全部精确匹配**——同 `resolve_npc_id` 的理由：模糊匹配是同义词打地鼠的
    开始（exec/17）。
    """
    key = (label or "").strip()
    if not key or key.casefold() == WORLD_SUBJECT:
        return WORLD_SUBJECT
    npc_id = resolve_npc_id(module, key)
    if npc_id is not None:
        return npc_id
    folded = key.casefold()
    for node in iter_all_nodes(module.nodes):
        if node.id.casefold() == folded or node.title.casefold() == folded:
            return node.id
    return None


def _entity_name_in_key(module: ScenarioModule, key: str) -> str | None:
    """世界级键里是不是塞进了某个实体的名字（`科比特态度` 这种）。

    🔴 代码判得了触发条件，但**不阻断**——阻断会把守秘人想记的东西整条丢掉，
    而它可能只是措辞习惯。记成 issue + 日志，让"还有多少条没挂对主体"变成
    可统计的量，将来要硬化时有据可依（exec/20 的一贯做法）。
    """
    for npc in module.npcs:
        if npc.name and npc.name in key:
            return npc.id
    for node in iter_all_nodes(module.nodes):
        if node.title and node.title in key:
            return node.id
    return None


async def update_state_impl(
    deps: KeeperDeps, key: str, value: str, subject: str = WORLD_SUBJECT
) -> tuple[str, str | None]:
    """写一条世界状态。返回 (执行报告, 问题描述或 None)。

    🔴 键的形状是 `<subject>.<key>`（世界级则只有 `key`）——见 `StateUpdate`
    的说明：没有主体的状态既不可裁剪也无法回答"谁看得见"（exec/24 §8.2）。
    """
    # write_lock：见 KeeperDeps 注释——SDK 并行工具调用下「读-改-写」必须串行。
    if key in deps.reserved_state_keys:
        raise KeeperToolError(f"状态键 {key!r} 由系统记账，不能通过 state_updates 写入")
    resolved = resolve_state_subject(deps.module, subject)
    if resolved is None:
        # 未知 id 一律拒绝，与 NPC/节点/议程/密级的处理一致：白名单外的东西
        # 不进状态，否则又回到"自由文本当标识符"。
        raise KeeperToolError(
            f"未知的状态主体 {subject!r}——必须是剧本里的 NPC id / 节点 id，"
            f"或世界级状态的 {WORLD_SUBJECT!r}"
        )
    issue: str | None = None
    if resolved == WORLD_SUBJECT and (hit := _entity_name_in_key(deps.module, key)) is not None:
        issue = f"状态键 {key!r} 里带了实体名，应挂在 subject={hit!r} 上"
        logger.info(
            "keeper_state_key_should_have_subject",
            room_id=deps.room_id,
            key=key,
            suggested_subject=hit,
        )
    stored_key = key if resolved == WORLD_SUBJECT else f"{resolved}.{key}"
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            raise KeeperToolError("房间不存在")
        current_state = room.keeper_state or {}
        # 🔴 时间不许倒流（2026-08-14）：这是**代码判得了**的记账错误，不是
        # 语义判断。此前时间是一个纯写给模型自己看的字符串，没有任何代码路径
        # 会因为它写错而出问题——「加了字段没有消费方 = 没加」。
        if stored_key == GAME_TIME_KEY and goes_backwards(current_state.get(stored_key), value):
            raise KeeperToolError(
                f"游戏内时间不能倒流：现在是 {current_state.get(stored_key)!r}，不能改成 {value!r}"
            )
        # ⚠️ JSON 列整体重新赋值（同 write_stat 的原因）。
        room.keeper_state = {**current_state, stored_key: value}
        await record_event(db, deps, "keeper.state", {"key": stored_key, "value": value})
    return f"已记录：{stored_key} = {value}", issue


async def _current_scene_name(deps: KeeperDeps) -> str | None:
    """本轮开始时 `keeper_state` 里记的「当前场景」。没有就是 None。"""
    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        value = (room.keeper_state or {}).get(SCENE_NAME_KEY) if room is not None else None
    return value.strip() if isinstance(value, str) else None


async def execute_state_updates(
    deps: KeeperDeps, decision: BaseModel, facts: TurnFacts
) -> tuple[list[str], list[str]]:
    """逐条记账。非法主体/保留键跳过并记 issue，不炸整轮。

    顺带 publish 一条本轮事实：裁决器有没有声明**新的**「当前场景」。`movement`
    要用它决定要不要清空节点指针（`exec/19 #48`），见 `TurnFacts` 的说明。
    **在这里设而不是在写库成功之后**：判定条件与切分前逐字一致（那时它读的是
    `decision.state_updates` 原始值，不管写没写成功）。

    🔴 **「写了」不等于「变了」**（2026-08-10 多人验证跑实锤）：裁决器几乎每轮
    都会把「当前场景」原样重写一遍，而这里第一版只看它写没写 → `movement` 每轮
    都以为换了场景 → 每轮清空位置表。真机后果是分头彻底失效：全房间位置一起
    掉成 None，None 是个**吸收态**（`group_players` 判成同一组），于是不但分头
    没了，连挂起的会合确认都被一起丢掉 = **没人点头就合并了**。
    字段的名字（`scene_name_declared`）和两处 docstring 说的都是"新场景"，
    只有实现没有比较新旧。
    """
    report: list[str] = []
    issues: list[str] = []
    previous_scene = await _current_scene_name(deps)
    for update in getattr(decision, "state_updates", ()):
        if update.key == SCENE_NAME_KEY and update.value.strip():
            if update.value.strip() != previous_scene:
                facts.scene_name_declared = update.value
            elif previous_scene is not None:
                # 明说了「还在原地」。跟"没提场景"是两回事——见 TurnFacts 的注释。
                facts.scene_name_restated = True
        try:
            line, issue = await update_state_impl(deps, update.key, update.value, update.subject)
            report.append(line)
            if issue is not None:
                issues.append(issue)
        except KeeperToolError as exc:
            issues.append(f"状态更新未执行：{exc}")
    return report, issues
