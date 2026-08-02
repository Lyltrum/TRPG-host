"""skill_check 能力贡献给裁决 prompt 的文本块。

🔴 文本是成品原文，不再经过 f-string / format，花括号写单个。

（规则 7「检定结果结算」不在这里：它同时管 checks 与 san_checks，讲的是
"结算轮该怎么裁"这条**流程**，跟规则 0/5/6 同类，归骨架。**共用的句子要么
改写让边界对齐，要么承认它根本不属于任何一片**——不能随手判给其中一方。）
"""

from app.core.keeper.registry import PromptBlock

PROMPT_BLOCKS = (
    PromptBlock(
        slot="rules",
        order=1.0,
        text="""1. **检定判定**：玩家不会替你喊技能名——判断"这个行动要不要检定、用哪个技能"是你的职责。玩家行动命中剧本标注的检定点时（搜索房间→侦察；打探/套话→话术/魅惑/信用；查资料→图书馆使用；跟踪痕迹→追踪），**必须**在 checks 里给出检定，技能从上面的权威 id 表里选、填 `skill_id`（填 id 不填中文名）；"我仔细翻找书房"就是完整的行动宣告，直接裁定侦察，不要求玩家先说明搜索方式。纯对话、无风险移动、观察显而易见之物不检定（checks 留空数组，理由写进 thinking）。**有检定时**：guidance 写到「需要掷骰的那一刻」为止，不要先写检定才能知道的结果。""",
    ),
    PromptBlock(
        slot="rules",
        order=1.4,
        text="""1b. **集体宣告要给每个人各发一次检定**：玩家说「**我们**打算…」「大家一起…」「我和 X 一块…」时，这是**全体在场调查员**的行动，不是发言者一个人的。该检定的话，checks 里要为**每一位参与的调查员各写一条**（`player` 逐个填昵称，不要只留一个 null）。真人实测 2026-07-31：玩家说"我们打算直接打他一顿"，只有发言者掷了斗殴，另一个人被晾在一边。""",
    ),
    PromptBlock(
        slot="rules",
        order=1.6,
        text="""1c. **对抗检定要用 opposed 字段，不要写进指引**：掰手腕、挣脱束缚、抵抗毒物、推门 vs 门后有人顶着——这类"你和对方比一把"的场合，在那条 check 里加 `"opposed": {"opponent": "科比特", "value": 80}`。`value` 是**百分位目标值 0-100**：NPC 的技能/属性直接用它的百分数，COC6 式的属性点（POT 16、STR 13）要 **×5** 换算（POT 16 → 80）。对手的骰子由系统掷、胜负由系统判，你**不要**在 narration_guidance 里写"请进行 XX 对抗检定"或自己宣布谁赢了——那样玩家界面上不会出现掷骰卡片，他会一直等一个不来的骰子。""",
    ),
    PromptBlock(
        slot="rules",
        order=2.0,
        text="""2. **玩家宣告技能时的合理性**：玩家点名的技能在当前情境不合理时（如用克苏鲁神话"看穿真相"），不要照单裁定——checks 留空，在 narration_guidance 里说明拒绝理由让叙事者转达。""",
    ),
    PromptBlock(
        slot="output_example",
        order=10,
        text='  "checks": [{"skill_id": "spot-hidden", "player": null, "reason": "搜索书房命中剧本检定点", "opposed": null}, {"skill_id": "CON", "player": "凌铭辉", "reason": "抵抗毒烟", "opposed": {"opponent": "毒烟", "value": 80}}]',
    ),
)
