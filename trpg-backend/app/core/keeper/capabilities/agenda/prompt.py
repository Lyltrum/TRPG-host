"""agenda 能力贡献给裁决 prompt 的文本块。

原规则 8 叫「议程与游戏内时间」，第①条（维护「游戏内时间」）其实属于
`world_state`——它写的是 `state_updates`。阶段 3 已经把那句摘成骨架的规则 4d
（跟其余状态记账规则待在一起，`world_state` 切走时一并带走），这里只剩议程，
子项顺次重编号。

🔴 判据同 health 的规则 3a：**共用的句子要改写文本让边界对齐，不能整段判给其中
一方**——判错了，下一个人就会去错的目录里找。
"""

from app.core.keeper.registry import PromptBlock

_RULE_AGENDA = PromptBlock(
    slot="rules",
    order=8.0,
    text="""8. **议程**：世界不只随玩家行动而动，还有自己的时间表。
   ①局面块的「议程状态」列出尚未发生的事件及其触发条件（自由文本描述）；你对照 keeper_state 的游戏内时间与当前局面，判断某条的触发条件是否在本轮达成，达成就把它的 id 写进 agenda_fired，并在 narration_guidance 里指示叙事者把这件事呈现出来；
   ②agenda_fired 只写"本轮真的发生了"的——不预告、不提前铺垫；
   ③已发生区里的事件不要再触发（once），也不要在叙事里当新事件重讲；
   ④议程事件**不依赖玩家在场**：玩家没去监视，事件照样发生，玩家事后才看到痕迹（这正是时间压力的来源）。""",
)

_OUTPUT_EXAMPLE = PromptBlock(
    slot="output_example",
    order=80,
    text='  "agenda_fired": ["some-agenda-id"]',
)

PROMPT_BLOCKS = (_RULE_AGENDA, _OUTPUT_EXAMPLE)
