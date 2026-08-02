"""progression 能力贡献给裁决 prompt 的文本块。

🔴 文本是成品原文，不再经过 f-string / format，花括号写单个。
"""

from app.core.keeper.contract.registry import PromptBlock

_RULE_PHASE = PromptBlock(
    slot="rules",
    order=10.0,
    text="""10. **对局阶段**：
   ①开场仪式（opening）：按剧本【开场脚本】建立委托与初始线索；一般不发起高风险检定；
     当委托/开场目标已建立时设 opening_complete=true（代码会推进到 investigation）；
   ②调查阶段：正常裁决；玩家已行动时优先 opening_complete=true 并进入实质调查；
   ③每轮顺带判断 endings[].trigger 是否满足——满足则 ending_reached 填该结局 id
     （代码收束对局）；未满足时 ending_reached 必须为 null。""",
)

_EXAMPLE_OPENING = PromptBlock(slot="output_example", order=100, text='  "opening_complete": false')
_EXAMPLE_ENDING = PromptBlock(slot="output_example", order=110, text='  "ending_reached": null')

PROMPT_BLOCKS = (_RULE_PHASE, _EXAMPLE_OPENING, _EXAMPLE_ENDING)
