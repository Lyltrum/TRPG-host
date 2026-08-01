"""规则原语：模组里 NPC 实体的寻址（把模型写的名字解析成白名单 id）。

**不属于任何一个能力**——health 要用它给 NPC 记血，world_state 要用它给
`state_updates.subject` 找主体。留在 health 里就会变成"world_state import
health"，那正是架构测试禁止的跨能力依赖（`exec/27`：能力之间不许互相 import，
共用的东西下沉）。

🔴 全部**精确**匹配，不做包含/模糊——`exec/17` 记过一次，模糊匹配是同义词
打地鼠的开始，正解是白名单。

（`dice` 与技能 id 白名单在阶段 5 目录终态时一并搬进这个包。）
"""

from __future__ import annotations

from app.core.keeper.module_loader import ScenarioModule


def resolve_npc_id(module: ScenarioModule, label: str) -> str | None:
    """把裁决器写的名字解析成模组里的 npc id。解析不出返回 None。

    匹配顺序：npc id → npc name → 形态 id → 形态 name。全部**精确**匹配
    （去空白、忽略大小写）。
    """
    key = (label or "").strip().casefold()
    if not key:
        return None
    for npc in module.npcs:
        if npc.id.casefold() == key or npc.name.casefold() == key:
            return npc.id
    for npc in module.npcs:
        for form in npc.forms:
            if form.id.casefold() == key or (form.name or "").casefold() == key:
                return npc.id
    return None


def npc_display_name(module: ScenarioModule, npc_id: str) -> str:
    for npc in module.npcs:
        if npc.id == npc_id:
            return npc.name
    return npc_id
