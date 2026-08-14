"""cast 能力贡献给裁决 prompt 的文本块。

🔴 文本是成品原文，不再经过 f-string / format，花括号写单个。
"""

from app.core.keeper.contract.registry import PromptBlock

#: 编号排在 `4f`（open_threads）之后。🔴 `4a`–`4f` 与 order 4.0/4.4/4.6/4.8/
#: 4.9/4.95 都已占用——第一版写了 `4e`，跟 `movement` 撞号，被
#: `test_no_rule_or_example_line_is_emitted_twice` 当场抓住。那条架构守护测试
#: 今天抓到我两次（另一次是 health 的 `3c` 撞 madness），它存在的意义就在这。
_RULE_CAST = PromptBlock(
    slot="rules",
    order=4.97,
    text=(
        "4g. **台上有谁**：每轮把此刻跟调查员在同一场景里的 NPC 写进 "
        "`npcs_on_stage`，填【登场 NPC】里的 **id**（不是名字、不许编）。"
        "这是**快照不是增量**：这一轮谁在就写谁，走掉的别再写，一个都没有就给空数组。\n"
        "   🔴 **叙事里出场的人必须能对上名册**。真人实测出过：路边小屋里那个"
        "「瘦小的酗酒老头」其实就是名册上的卡比·卡普顿，而模型没把两者联系起来，"
        "下一轮居然让他指路去找他自己。写下 id 就是在回答「跟玩家说话的到底是谁」——"
        "名册里确实没有的过路人不用写（也写不进去），但**只要是名册上的人就必须写**。"
    ),
)

#: 输出示例的 order 同样得避开：45/46 被 open_threads 占着。
_OUTPUT_EXAMPLE = PromptBlock(
    slot="output_example",
    order=47,
    text='  "npcs_on_stage": ["cappy-capton"]',
)

PROMPT_BLOCKS = (_RULE_CAST, _OUTPUT_EXAMPLE)
