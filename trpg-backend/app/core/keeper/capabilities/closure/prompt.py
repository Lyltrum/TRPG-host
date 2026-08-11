"""closure 能力贡献给裁决 prompt 的文本块。

🔴 文本是成品原文，不再经过 f-string / format，花括号写单个。
"""

from app.core.keeper.contract.registry import PromptBlock

_RULE_CLOSURE = PromptBlock(
    slot="rules",
    order=10.5,
    text="""10b. **没有预设结局时的自然收尾**：局面块「还剩多少内容」三个数都见底
   （没揭开的线索、没触发的一次性议程、没去过的地方），而玩家这一轮也确实
   走到了一个停得住的地方 —— 写 `story_ran_its_course=true`。
   ①**模组有结局候选时优先 `ending_reached`**，那是剧本自己写的落幕；
   ②还在揭开新线索、还有一次性议程没发生时**不许**写 true（代码也会拦）；
   ③写了 true 之后，narration_guidance 里正常收尾即可 —— 怎么把故事停下来
     就是这一段叙事的事，不需要额外字段。""",
)

_EXAMPLE_CLOSURE = PromptBlock(
    slot="output_example", order=115, text='  "story_ran_its_course": false'
)

PROMPT_BLOCKS = (_RULE_CLOSURE, _EXAMPLE_CLOSURE)
