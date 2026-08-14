"""台上此刻有谁：存储形态 + 局面块。

## 它补的是哪个洞（2026-08-14 真人实测）

玩家走进路边小屋，叙事写「一个瘦小的老头坐在木箱上，手里攥着半瓶酒」。
下一轮玩家捡起报纸，上面写着居民「卡比」·卡普顿声称看见会飞的牛。玩家问
「你知道这个卡比是什么吗」，主持人让**老头笑着回答「卡比就住在两英里外」**。

屋里那个酗酒老头**就是卡比**（`cappy-capton`，模组名册里有他）。模型把同一个
人当成了两个，还让他指路去找他自己。玩家后来付的酒钱、拿到的方向，全建立在
这个错位上。

## 根因：位置有 id、线索有 id、悬而未决有 id，**唯独台上的人没有**

模组名册（`登场 NPC`）一直在 system prompt 里，但那是**剧本里有谁**；
"此刻跟你说话的是名册上的哪一个"从来没有被记下来过。叙事里的 NPC 只是一段
自由文本，下一轮模型读到的还是那段散文——于是「一个老头」与「卡比」成了
两条互不相干的信息。

这是「不要用自由文本当标识符」在 NPC 维度上的复发。

## 形态：照抄即兴地点 / 悬而未决

**这是那套形态的第三个实例**，所以照抄：模型只能从**白名单**里挑 id
（这里的白名单就是模组名册，不需要代码分配 id——NPC 的 id 是模组给的），
局面块全量列出此刻在场的人。

跟 `NPC状态`（health 那片）分得很清，别混：那张表记的是**伤成什么样**，
这里记的是**谁在台上**。一份数据扮演两个角色必出结构性 bug。
"""

from __future__ import annotations

from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.contract.registry import SituationContext
from app.core.keeper.primitives.npcs import npc_display_name

#: 此刻在台上的 NPC id，逗号分隔。由本能力的 `reserved_state_keys` 声明出去。
ON_STAGE_KEY = "在场NPC"


def load_on_stage(keeper_state: dict | None) -> list[str]:
    """解析在场 NPC id 列表，保序去重。"""
    if not keeper_state:
        return []
    raw = keeper_state.get(ON_STAGE_KEY)
    if raw is None or raw == "":
        return []
    out: list[str] = []
    for part in str(raw).split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def serialize_on_stage(npc_ids: list[str]) -> str:
    return ", ".join(npc_ids)


def format_on_stage(module: ScenarioModule, keeper_state: dict | None) -> str:
    """局面块正文。台上没人时返回空串——整块不渲染（退化保证）。"""
    on_stage = load_on_stage(keeper_state)
    if not on_stage:
        return ""
    lines = [f"- {npc_id}：{npc_display_name(module, npc_id)}" for npc_id in on_stage]
    return (
        "此刻跟调查员在同一个场景里的是**这几个人**，"
        "叙事里提到他们时**必须用这里的名字**，不要另起称呼"
        "（「那个老头」「酒鬼」这类含糊指代会让同一个人在下一轮变成两个人）：\n"
        + "\n".join(lines)
        + "\n"
        "🔴 他们**已经登场了**——不要再让他们互相介绍、也不要让其中一个去打听另一个"
        "（真人实测出过：屋里那个老头就是玩家要找的卡比，主持人却让他指路去找自己）。"
    )


def render_on_stage(context: SituationContext) -> str:
    return format_on_stage(context.module, context.keeper_state)
