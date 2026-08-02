"""把「可能的结局与触发条件」摆到裁决器眼前。"""

from __future__ import annotations

from app.core.keeper.registry import SituationContext


def format_endings_status(context: SituationContext) -> str:
    """每轮注入的「可能的结局与触发条件」（exec/19 #47）。

    试玩实测 2026-08-01：最后一轮叙事已经完整写出了结局（警察进屋、FBI 封锁、
    烧掉房子、官方声明），而 `phase` 仍是 investigation、`ending_id` 仍是 None
    ——**故事结束了，对局没结束**，系统还在等下一轮。

    收束靠裁决器写 `ending_reached`，而结局条件此前只存在于 system prompt 里
    那份剧本全文的末尾。议程能被可靠触发，正是因为它每轮都以「议程状态」小节
    出现在局面块里、就在眼前。这里给结局同样的待遇——**把该判断的东西摆到
    它面前**，比在规则里多写一句"记得判断"可靠。

    ⚠️ 如实说：这仍是概率性改进。"这段剧情算不算命中结局"是纯语义判断，
    没有代码手段能确定性地判定它。

    只用到 `context.module`——结局条件来自剧本，不随对局状态变。
    """
    module = context.module
    if not module.endings:
        return ""
    lines = []
    for ending in module.endings:
        trigger = (ending.trigger or ending.condition or "").strip()
        lines.append(f"- {ending.id} · {ending.title}：{trigger or '（未写触发条件）'}")
    return "\n".join(lines)
