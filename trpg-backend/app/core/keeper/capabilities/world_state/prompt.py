"""world_state 能力贡献给裁决 prompt 的文本块。

🔴 文本是成品原文，不再经过 f-string / format，花括号写单个。
"""

from app.core.keeper.registry import PromptBlock

_RULE_STATE = PromptBlock(
    slot="rules",
    order=4.0,
    text="""4. **状态记账**：本轮有实质进展时（进入新场景、关键线索被挣得、NPC 态度变化、游戏内时间流逝）写 state_updates——这是跨轮记忆的唯一来源。
   🔴 **每条都要挂主体**：`subject` 填这条状态属于哪个 NPC/节点的 **id**（取自下面剧本里的 `id: xxx`），不属于任何具体实体的（游戏内时间、天气、委托整体进度）填 `world`。
   **不要把主体名字写进 key**——写 `{"subject": "butler-public", "key": "态度", "value": "警觉"}`，不要写 `{"subject": "world", "key": "管家态度"}`。前者下一轮还能被认出来是同一件事，后者换个措辞就变成两条并存的记录。玩家移动后**必须**更新「当前场景」（state_updates 里的人类可读地名），**并且**把 current_node_id 设为剧本节点列表中对应的 id（每个节点标题后括号里的"id: xxx"）；找不到精确对应的节点时 current_node_id 留空（null），禁止编造不存在的 id。""",
)

#: 从原规则 8①（议程那段）摘出来的——它写的是 state_updates，属于本能力。
_RULE_GAME_TIME = PromptBlock(
    slot="rules",
    order=4.8,
    text='4d. **游戏内时间**：每轮维护 keeper_state 的「游戏内时间」（如"第2天 夜晚"）——用 state_updates 写（subject 填 world），剧情推进到新的时段就更新。议程事件靠它判断触发时机。',
)

_OUTPUT_EXAMPLE = PromptBlock(
    slot="output_example",
    order=40,
    text='  "state_updates": [{"subject": "world", "key": "当前场景", "value": "书房"}]',
)

PROMPT_BLOCKS = (_RULE_STATE, _RULE_GAME_TIME, _OUTPUT_EXAMPLE)
