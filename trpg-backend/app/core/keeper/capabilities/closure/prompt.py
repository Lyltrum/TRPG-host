"""closure 能力贡献给裁决 prompt 的文本块。

🔴 文本是成品原文，不再经过 f-string / format，花括号写单个。
"""

from app.core.keeper.contract.registry import PromptBlock

_RULE_CLOSURE = PromptBlock(
    slot="rules",
    order=10.5,
    text="""10b. **没有预设结局时的自然收尾**：局面块「这一局还剩多少内容」里**门槛
   那两行都归零了**（没揭开的线索配对 = 0、没触发的一次性议程 = 0），而玩家
   这一轮也确实走到了一个停得住的地方 —— 写 `story_ran_its_course=true`，并在
   narration_guidance 里把故事停到一个停得住的地方。
   ①**模组有结局候选时优先 `ending_reached`**，那是剧本自己写的落幕；
   ②门槛两行还没归零、或者本轮刚揭开新线索时**不许**写 true（代码也会拦）；
   ③「没去过的地方」和「无进展轮数」**不是**收尾依据 —— 它们大只说明这桌人
     还在打转，那时候该**给推力**（丢线索、让议程事件闯进来、NPC 上门），
     不是收场；
   ④**收早了不要紧**：写了 true 只是进入收尾，玩家只要接着行动就自动回到
     调查阶段，你再接着主持即可。""",
)

_EXAMPLE_CLOSURE = PromptBlock(
    slot="output_example", order=115, text='  "story_ran_its_course": false'
)

PROMPT_BLOCKS = (_RULE_CLOSURE, _EXAMPLE_CLOSURE)
