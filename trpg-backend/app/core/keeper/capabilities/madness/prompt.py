"""madness 能力贡献给裁决 prompt 的文本块。"""

from app.core.keeper.contract.registry import PromptBlock

#: 紧跟在规则 3 一族后面：疯狂是理智损失的直接后果，读的时候要连着读。
#: 编号沿用骨架约定（`a`→+0.2、`b`→+0.4），3a/3b 已被 health 占用 ⇒ 3c。
#:
#: 🔴 这一条**只讲解除**。发疯由代码强制（损失≥5 当场落库、症状代码掷），
#: 在规则里再说一遍"该让他发疯"只会让模型以为那是它的活儿，然后自己在叙事
#: 里另编一种症状——跟局面块里那条打架。
_RULE_MADNESS = PromptBlock(
    slot="rules",
    order=3.6,
    text="""3c. **疯狂**：谁在疯、疯的是哪一种，由系统判定并列在局面块「疯狂中的调查员」里，**你不要自己让人发疯、也不要改症状**。
   那些人的行动一律带着自己的症状演，直到他缓过来。**他确实缓过来了**（同伴安抚成功、离开了刺激源、已经过了相当长一段时间）才写 `madness_recovered`——不写就一直疯着，而一直疯着是错的。""",
)

_OUTPUT_EXAMPLE = PromptBlock(
    slot="output_example",
    order=25,
    text='  "madness_recovered": [{"player": "阿福", "reason": "被同伴按住肩膀劝了半天"}]',
)

PROMPT_BLOCKS = (_RULE_MADNESS, _OUTPUT_EXAMPLE)
