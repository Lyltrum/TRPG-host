"""clue_reveal 能力贡献给裁决 prompt 的文本块。

🔴 文本是成品原文，不再经过 f-string / format，花括号写单个。
"""

from app.core.keeper.registry import PromptBlock

_RULE_CLUES = PromptBlock(
    slot="rules",
    order=9.0,
    text="""9. **密级配对（Visibility）**：局面块的「密级配对状态」列出尚未揭开 / 已揭开的 pair。
   玩家通过成功检定或明确剧情挣得 public 侧信息时，把对应 pair 的 id 写入 clues_revealed；
   未揭开的 secret_ref 侧内容禁止写进 narration_guidance 的"可揭示"清单。""",
)

_OUTPUT_EXAMPLE = PromptBlock(
    slot="output_example", order=90, text='  "clues_revealed": ["pair-id"]'
)

PROMPT_BLOCKS = (_RULE_CLUES, _OUTPUT_EXAMPLE)
