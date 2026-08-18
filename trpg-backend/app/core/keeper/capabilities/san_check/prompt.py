"""san_check 能力贡献给裁决 prompt 的文本块。"""

from app.core.keeper.contract.registry import PromptBlock

#: 原规则 3「理智/伤害」拆开后属于本能力的那半句（health 拿走了 3a 伤害）。
_RULE_SAN = PromptBlock(
    slot="rules",
    order=3.0,
    text="""3. **理智**：局面块「理智检定点」列出的是模组标注、本局还没掷过的——玩家在这一轮**目睹**对应之物时**必须**发起 san_checks，损失表达式照抄那里的数值。
   模组没标、但按 COC7 规则该掷的**也要掷**：首次目睹超自然生物或超自然现象（典型 0/1D6）、见到尸体（0/1D3）、血肉模糊的现场（1/1D6）。**玩家说出"这还是人类吗""我不敢相信眼前的东西"这类反应，正是该掷的信号**，不要因为"这一轮没推进场景"就跳过。
   🔴 **同一来源不重复检定**：同一个怪物、同一具尸体见过一次就不再掷。局面块「最近的理智检定」列着你刚才为什么掷过，对着看。
   **"它又动了一下、又靠近了一点、又被看清了一点"不算升级**——那是同一个东西的下一秒，而且那句描写正是你自己上一拍写的。真正的新来源是它变成了**另一样东西**：尸体站起来走路，第一次看见是新的（死人本不该动）；它站起来之后迈步、伸手、开口，全都还是它。数量从一只变成一片、或者出现了另一个此前没见过的东西，才是另一次。
   日常紧张、打斗受伤、天黑迷路都不掷。""",
)

_OUTPUT_EXAMPLE = PromptBlock(
    slot="output_example",
    order=20,
    text='  "san_checks": [{"player": null, "loss_on_success": "0", "loss_on_failure": "1d6", "reason": "目击食尸鬼"}]',
)

PROMPT_BLOCKS = (_RULE_SAN, _OUTPUT_EXAMPLE)
