"""agenda 能力贡献给裁决 prompt 的文本块。

⚠️ 如实记：规则 8 的第①条（维护「游戏内时间」）其实属于 `world_state`——它写的
是 `state_updates`。整条规则跟着 agenda 走是因为剩下四条都是议程，而**把一句话
从别人的段落里摘出来要改字面**，那要重录磁带。跟规则 3「理智/伤害」同一类，
一起放在阶段 3 收尾的那次文本清理里处理。
"""

from app.core.keeper.registry import PromptBlock

_RULE_AGENDA = PromptBlock(
    slot="rules",
    order=8.0,
    text="""8. **议程与游戏内时间**：世界不只随玩家行动而动，还有自己的时间表。
   ①每轮维护 keeper_state 的「游戏内时间」（如"第2天 夜晚"）——用 state_updates 写（subject 填 world），剧情推进到新的时段就更新；
   ②局面块的「议程状态」列出尚未发生的事件及其触发条件（自由文本描述）；你对照 keeper_state 的游戏内时间与当前局面，判断某条的触发条件是否在本轮达成，达成就把它的 id 写进 agenda_fired，并在 narration_guidance 里指示叙事者把这件事呈现出来；
   ③agenda_fired 只写"本轮真的发生了"的——不预告、不提前铺垫；
   ④已发生区里的事件不要再触发（once），也不要在叙事里当新事件重讲；
   ⑤议程事件**不依赖玩家在场**：玩家没去监视，事件照样发生，玩家事后才看到痕迹（这正是时间压力的来源）。""",
)

_OUTPUT_EXAMPLE = PromptBlock(
    slot="output_example",
    order=80,
    text='  "agenda_fired": ["some-agenda-id"]',
)

PROMPT_BLOCKS = (_RULE_AGENDA, _OUTPUT_EXAMPLE)
