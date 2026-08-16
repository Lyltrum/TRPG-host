"""established 的 prompt 块。"""

from __future__ import annotations

from app.core.keeper.contract.registry import PromptBlock

_RULE = PromptBlock(
    slot="rules",
    order=4.75,
    text=(
        "4h. **已经成了定局的事**：调查员做了某件**不可逆**的事、而它的后果会一直"
        "为真——烧掉了一栋房子、杀死或救活了一个人、把关键证物交给了警察、跟某个"
        'NPC 彻底翻脸——写进 `new_facts`：`[{"text": "调查员烧掉了林中的木屋"}]`。\n'
        "   🔴 **跟 `new_threads` 的区别是「完了没有」**，不是重要程度：还在持续、"
        "需要你接着演的处境（追兵还在后面、门被反锁了）写 new_threads，它之后可以"
        "用 resolved_threads 结清；**已经结束但后果永久**的写这里，它没有结清动作、"
        "会一直留着。\n"
        "   🔴 **不要拿它记这些**：位置、线索、随身物品、谁在台上、疯狂、生命值——"
        "系统都已经记账并摆在局面块里了，在这里再记一遍就是两份对不上的账。也不要"
        "记只影响这一段叙事的细节（那种直接写进 narration_guidance）。\n"
        "   写成**已完成的事实**（「木屋已经烧毁」），不要写成还在进行的动作。"
    ),
)

_EXAMPLE = PromptBlock(
    slot="output_example",
    order=76,
    text='  "new_facts": []',
)

PROMPT_BLOCKS = (_RULE, _EXAMPLE)
