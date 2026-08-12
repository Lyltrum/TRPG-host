"""「疯狂中的调查员」局面块：谁在疯、疯的是哪一种、要怎么演。

这一块是这条线的**落点**：症状不做成 id 的话，它只会活在触发那一轮的散文里，
下一轮模型就忘了（同 `exec/31` 那条「即兴出来的东西没有落点」）。
"""

from __future__ import annotations

from app.core.keeper.contract.registry import SituationContext
from app.core.keeper.runtime.madness_state import load_madness, symptom_by_id


def format_madness(context: SituationContext) -> str:
    """没人在疯狂中就返回空串——整块不渲染。

    拿不到规则数据（`ruleset=None`）时同样返回空串：那时 symptom_id 翻不成
    症状名，**宁可整块不渲染也不打印裸 id** ——模型读到一个它没见过的英文
    id，只会照着字面现编一种发作表现，比不告诉它更糟。
    """
    if context.ruleset is None:
        return ""
    madness = load_madness(context.keeper_state)
    if not madness:
        return ""
    nicknames = dict(context.players)
    lines: list[str] = []
    for player_id, symptom_id in madness.items():
        symptom = symptom_by_id(context.ruleset, symptom_id)
        if symptom is None:
            # 规则表换过了、这条 id 查不到。跳过而不是打印 id：同上。
            continue
        who = nicknames.get(player_id)
        if who is None:
            # 这个人已经不在场（离开房间）。他的记录留着（回来还算数），
            # 但这一轮没有人需要看见它。
            continue
        lines.append(f"- {who}：{symptom.label}——{symptom.description}")
    if not lines:
        return ""
    return (
        "下面这些调查员正处在临时性疯狂中。他们的**每一次行动都要带上这个症状**，"
        "不要写成他们已经冷静下来了。\n"
        "他确实缓过来了（同伴安抚成功、脱离了刺激源、过了足够长的时间）时，"
        "必须写 `madness_recovered`——不写就一直疯着。\n" + "\n".join(lines)
    )
