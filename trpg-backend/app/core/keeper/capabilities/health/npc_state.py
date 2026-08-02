"""NPC 的对局内状态（exec/19 #39）。

## 为什么需要它

模组数据里 `npcs[].stats` / `forms[]` 是**静态数据卡**——开局时它是什么样。
战斗一开打，裁决器很合理地想记"科比特被打掉 4 点 HP"，而 `adjust_hp_impl`
只认房间里的**玩家**，于是：

    keeper_decision_issues issues=['HP 变更未执行：找不到玩家「科比特」]

NPC 的伤势完全没有落点，只活在叙事文字里；下一轮裁决器无从得知它伤到什么
程度，只能凭上一段散文猜。这跟 #38 是同一族问题——**系统接不住的东西，
模型就只能写进散文**。

## 存储形态与寻址

存在 `keeper_state[NPC_STATE_KEY]`，`npc_id` → 状态字典。**键必须是模组里
的 npc id**（白名单），不是裁决器随口写的名字——自由文本当标识符会退化成
同义词打地鼠（项目 CLAUDE.md 判据，先例 `current_node_id`）。解析在
`primitives/npcs.resolve_npc_id`（world_state 也要用它，所以不属于本能力），
都不中就报错、**不新建条目**。

## HP 缺数据时怎么办

`stats` 是自由字典，不同怪物的数据轴不一样，很多 NPC 压根没有 HP 那一行。
这时**不编一个初始值**（静默兜底是同一个 bug 反复换皮的元凶），而是显式
降级成累计伤害：状态里记 `damage`，渲染成「累计受伤 4 点（数据卡无 HP）」。
裁决器看得出这是"伤了多少"而不是"还剩多少"，不会被假的血条误导。
"""

from __future__ import annotations

from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.contract.registry import SituationContext
from app.core.keeper.primitives.npcs import npc_display_name

NPC_STATE_KEY = "NPC状态"

#: 数据卡里可能承载"生命值"的键。COC7 的数据卡写法不统一（英文缩写/中文/
#: 全称都有），这里只认这几种**精确**写法——认不出就走累计伤害那条显式降级，
#: 不做模糊匹配（模糊匹配会把 "HP_MAX"、"HPS" 这类也认成 HP）。
_HP_KEYS = ("HP", "hp", "耐久", "生命", "生命值")


def initial_hp(module: ScenarioModule, npc_id: str) -> int | None:
    """数据卡上的 HP。没有这一行就返回 None（调用方走累计伤害那条降级）。"""
    for npc in module.npcs:
        if npc.id != npc_id:
            continue
        for key in _HP_KEYS:
            raw = (npc.stats or {}).get(key)
            if raw is None:
                continue
            try:
                return int(str(raw).strip())
            except ValueError:
                # 数据卡上写的是 "12（形态二 20）"这类自由文本 → 当作没有
                return None
    return None


def load_npc_states(keeper_state: dict | None) -> dict[str, dict]:
    if not keeper_state:
        return {}
    raw = keeper_state.get(NPC_STATE_KEY)
    if not isinstance(raw, dict):
        return {}
    return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}


def apply_hp_delta(
    states: dict[str, dict], npc_id: str, delta: int, *, base_hp: int | None
) -> dict:
    """记一笔 NPC 的 HP 变化，返回**这个 NPC 更新后**的状态。

    有数据卡 HP → 维护 `hp`（下限 0）；没有 → 维护 `damage`（累计伤害，下限 0，
    正数表示受伤多少）。两条路各自自洽，不互相假装成对方。
    """
    state = dict(states.get(npc_id) or {})
    if base_hp is not None:
        current = state.get("hp")
        current = base_hp if not isinstance(current, int) else current
        state["hp"] = max(0, current + delta)
        state.pop("damage", None)
    else:
        current = state.get("damage")
        current = 0 if not isinstance(current, int) else current
        state["damage"] = max(0, current - delta)
    states[npc_id] = state
    return state


def format_npc_states(context: SituationContext) -> str:
    """注入局面块的「NPC 当前状态」。没有任何记录时返回空串（整块不渲染）。"""
    module = context.module
    states = load_npc_states(context.keeper_state)
    if not states:
        return ""
    lines: list[str] = []
    for npc_id, state in states.items():
        name = npc_display_name(module, npc_id)
        if isinstance(state.get("hp"), int):
            hp = state["hp"]
            suffix = "（已倒地/失去行动力）" if hp == 0 else ""
            lines.append(f"- {name}（{npc_id}）：HP {hp}{suffix}")
        elif isinstance(state.get("damage"), int):
            lines.append(f"- {name}（{npc_id}）：累计受伤 {state['damage']} 点（数据卡无 HP）")
    return "\n".join(lines)
