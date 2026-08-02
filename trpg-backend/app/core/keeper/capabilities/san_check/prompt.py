"""san_check 能力贡献给裁决 prompt 的文本块。"""

from app.core.keeper.contract.registry import PromptBlock

#: 原规则 3「理智/伤害」拆开后属于本能力的那半句（health 拿走了 3a 伤害）。
_RULE_SAN = PromptBlock(
    slot="rules",
    order=3.0,
    text="3. **理智**：目击恐怖之物按剧本的损失表达式给 san_checks。剧本没有要求时不要凭空扣减。",
)

_OUTPUT_EXAMPLE = PromptBlock(
    slot="output_example",
    order=20,
    text='  "san_checks": [{"player": null, "loss_on_success": "0", "loss_on_failure": "1d6", "reason": "目击食尸鬼"}]',
)

PROMPT_BLOCKS = (_RULE_SAN, _OUTPUT_EXAMPLE)
