"""inventory 能力贡献给裁决 prompt 的文本块。"""

from app.core.keeper.contract.registry import PromptBlock

PROMPT_BLOCKS = (
    PromptBlock(
        slot="rules",
        order=3.8,
        text=(
            "3d. **随身物品也会变**：叙事里调查员拿到、买到、捡到、被给予一件东西时，"
            '写 `equipment_changes`（`{"player": null, "gained": ["撬棍"], '
            '"lost": [], "reason": "从工具棚里拿的"}`）；被夺走、用光、损毁就写进 `lost`，'
            "名字要跟【角色卡】随身那一行上的**一字不差**（对不上系统会拒绝，那件东西不会消失）。\n"
            "   🔴 **玩家用到随身清单上没有的东西时，先问他哪来的**，不要默认他有。"
            "「随身：未列」就是字面意思——他身上什么都没有。"
            "真人实测 2026-08-14：叙事写调查员开枪，而他的随身是空的，枪从头到尾没有出处。\n"
            "   衣服、鞋子这类不必记；**能改变一个场面成不成立的**才记（光源、武器、"
            "撬具、绳子、药品、钥匙、相机）。"
        ),
    ),
    PromptBlock(
        slot="output_example",
        order=47,
        text='  "equipment_changes": []',
    ),
)
