"""closure 能力贡献给裁决 prompt 的文本块。

🔴 文本是成品原文，不再经过 f-string / format，花括号写单个。
"""

from app.core.keeper.contract.registry import PromptBlock

_RULE_CLOSURE = PromptBlock(
    slot="rules",
    order=10.5,
    text="""10b. **没有预设结局时的自然收尾**：你是这一桌的守秘人，**什么时候该收尾
   由你判断** —— 就像真人 KP 那样。觉得这个故事已经讲完了、或者这帮人已经
   不会再往前走了，就写 `story_ran_its_course=true`，并在 narration_guidance
   里把故事停到一个停得住的地方。
   ①**模组有结局候选时优先 `ending_reached`**，那是剧本自己写的落幕；
   ②局面块的「还剩多少内容」与「无进展轮数」是**参考材料，不是门槛** ——
     内容没跑完也可以收（玩家在原地打转就是最常见的一种），内容跑完了也可以
     再等一等；
   ③唯一不许的是**自相矛盾**：本轮刚揭开新线索又同时说故事完了（代码会拦）；
   ④**收早了不要紧**：写了 true 只是进入收尾，玩家只要接着行动就自动回到
     调查阶段，你再接着主持即可。宁可试着收一次，也不要让一桌人无限循环。""",
)

_EXAMPLE_CLOSURE = PromptBlock(
    slot="output_example", order=115, text='  "story_ran_its_course": false'
)

PROMPT_BLOCKS = (_RULE_CLOSURE, _EXAMPLE_CLOSURE)
