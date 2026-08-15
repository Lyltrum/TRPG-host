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

import re

from app.core.keeper.contract.module_loader import ScenarioModule


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


#: 数据卡上"这一项是个属性点"的键。COC7 里属性点要 ×5 才是百分位目标值，
#: 而攻击项（"爪击 70% 1D6"）本身就写成了百分数。两类换算不同，只能逐个列出
#: ——**这是个「逐个列出的地方」**，加一条属性轴要回来加一行。
_ATTRIBUTE_KEYS = ("STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU", "LUCK", "POT")

#: 攻击项写法不统一，实测四只米-戈就有三种：
#:   "70% 1D6+1D6 有几率抓住调查员" / "55%, 1D6伤害，…" / "40%（伤害值见上文）"
#: 共同点是**百分数在最前面**，所以只认这一种形状：开头的数字 + %。
_LEADING_PERCENT = re.compile(r"^\s*(\d{1,3})\s*%")


def npc_ability_names(module: ScenarioModule, npc_id: str) -> list[str]:
    """这个 NPC 数据卡上有哪些项。裁决器只能从这里面挑，挑不中就是编造。"""
    for npc in module.npcs:
        if npc.id == npc_id:
            return list((npc.stats or {}).keys())
    return []


def npc_check_target(module: ScenarioModule, npc_id: str, ability: str) -> int | None:
    """NPC 拿哪个数掷。取不到就返回 None——**由调用方拒绝这次检定**。

    🔴 **取不到就不掷，不猜**（用户 2026-08-15 拍板的不对称）：名册里有数据卡
    的 NPC 用它自己的数值真掷；即兴造出来的 NPC 没有数据卡，那一格就该空着，
    由叙事直接裁定。让裁决器现编一个目标值等于把难度交给模型自己定，正是
    「能确定化的是判断的**输入**，不是判断本身」要避免的。

    两种取值方式，按数据卡上那一项自己的形状分：

    - **属性点**（`STR 15`）→ ×5 换成百分位（COC7）；
    - **攻击项**（`爪击 70% 1D6+1D6 …`）→ 取开头那个百分数。

    键名要求**一字不差**：`ability` 是从数据卡里挑的，而数据卡整段就摆在裁决
    器眼前（`render_npc`）。做模糊匹配就又回到同义词打地鼠（exec/17）。
    """
    for npc in module.npcs:
        if npc.id != npc_id:
            continue
        raw = (npc.stats or {}).get(ability)
        if raw is None:
            return None
        if ability in _ATTRIBUTE_KEYS:
            try:
                return max(1, min(100, int(str(raw).strip()) * 5))
            except ValueError:
                return None
        matched = _LEADING_PERCENT.match(str(raw))
        if matched is None:
            return None
        return max(1, min(100, int(matched.group(1))))
    return None
