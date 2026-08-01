"""health 能力贡献给裁决 prompt 的文本块。

原规则 3 是「理智/伤害」一句话，**san_check 与 health 共用**——阶段 2 因为
"prompt 逐字节不变"的硬要求没敢动它。阶段 3 拆开了：骨架的规则 3 只讲理智
（等 `san_check` 切走时跟它一起走），伤害归这里的 3a。

🔴 **能力边界不是每一处都恰好落在文本边界上。** 遇到这种共用句子，正解是改写
文本让边界对齐，而不是把整段判给其中一方——判错了，下一个人就会去错的目录里
找。代价是重录一次磁带，值得。

🔴 这里的文本是**成品原文，不再经过 f-string / format**，所以花括号写单个，
不写 `{{`。骨架把它整段插进自己的 f-string 占位符里，插进去之后不会再有
一次转义。
"""

from app.core.keeper.registry import PromptBlock

#: 从骨架规则 3 拆出来的「伤害」那半句。order 约定：`a` → +0.2。
_RULE_DAMAGE = PromptBlock(
    slot="rules",
    order=3.2,
    text="3a. **伤害**：受到伤害给 hp_changes。剧本没有要求时不要凭空扣减。",
)

#: 规则 3b 紧跟其后。order 沿用骨架的编号约定：`b` → +0.4。
_RULE_NPC_HP = PromptBlock(
    slot="rules",
    order=3.4,
    text=(
        '3b. **NPC 也会掉血**：伤到的是 NPC/怪物时，hp_changes 写 `npc` 字段而不是 `player`（`{"delta": -4, "npc": "科比特", "reason": "被铁铲砍中"}`）。名字必须是上面【登场 NPC】里的名字或 id，不要另起称呼。局面块有「NPC 当前状态」小节时那是**权威值**，按它裁决，不要从上一段叙事里猜它伤到什么程度。'
    ),
)

#: 输出格式示例里的那一行（不带行尾逗号，逗号由骨架拼接时统一加）。
_OUTPUT_EXAMPLE = PromptBlock(
    slot="output_example",
    order=30,
    text='  "hp_changes": [{"delta": -2, "player": null, "reason": "被抓伤"}, {"delta": -4, "npc": "科比特", "reason": "被铁铲砍中"}]',
)

PROMPT_BLOCKS = (_RULE_DAMAGE, _RULE_NPC_HP, _OUTPUT_EXAMPLE)
