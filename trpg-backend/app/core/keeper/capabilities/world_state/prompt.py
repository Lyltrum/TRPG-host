"""world_state 能力贡献给裁决 prompt 的文本块。

🔴 文本是成品原文，不再经过 f-string / format，花括号写单个。
"""

from app.core.keeper.contract.registry import PromptBlock

_RULE_STATE = PromptBlock(
    slot="rules",
    order=4.0,
    text="""4. **状态记账**：本轮有实质进展时（进入新场景、关键线索被挣得、NPC 态度变化、游戏内时间流逝）写 state_updates——这是跨轮记忆的唯一来源。
   🔴 **每条都要挂主体**：`subject` 填这条状态属于哪个 NPC/节点的 **id**（取自下面剧本里的 `id: xxx`），不属于任何具体实体的（游戏内时间、天气、委托整体进度）填 `world`。
   **不要把主体名字写进 key**——写 `{"subject": "butler-public", "key": "态度", "value": "警觉"}`，不要写 `{"subject": "world", "key": "管家态度"}`。前者下一轮还能被认出来是同一件事，后者换个措辞就变成两条并存的记录。
   🔴 **key 只能从这两张清单里挑**：世界级（subject=world）只有「当前场景」和「游戏内时间」；挂在 NPC/节点上的只有「态度」「状态」「进度」。**别的一律写不进去**，因为——会持续影响后续的处境请写 `new_threads`（有 id，之后可以用 `resolved_threads` 结清）；位置、线索、随身物品、谁在台上、疯狂、生命值**都由系统记账、并且已经摆在局面块里了**，在这里再记一份只会变成两份对不上的账；只影响这一段叙事的细节直接写进 narration_guidance，不要落成状态。玩家移动后**必须**更新「当前场景」（state_updates 里的人类可读地名），**并且**把 current_node_id 设为剧本节点列表中对应的 id（每个节点标题后括号里的"id: xxx"）；找不到精确对应的节点时 current_node_id 留空（null），禁止编造不存在的 id。""",
)

#: 从原规则 8①（议程那段）摘出来的——它写的是 state_updates，属于本能力。
#:
#: 🔴 **2026-08-14 补了"什么时候该推"**：原文只说"推进到新时段就更新"，而
#: "算不算新时段"没有任何抓手，实测一整局只更新过一次（之后跑了 5 个地方、
#: 等了一小时、开了两小时车，时间仍停在「第2天 清晨」）。这里给的是几条**能
#: 对照着数**的事：赶路、等待、过夜、休息。倒流由代码直接拒（`game_time.py`）。
_RULE_GAME_TIME = PromptBlock(
    slot="rules",
    order=4.8,
    text=(
        "4d. **游戏内时间**：每轮维护 keeper_state 的「游戏内时间」"
        "（形如「第2天 夜晚」）——用 state_updates 写（subject 填 world）。"
        "议程事件靠它判断触发时机，**它不动，那些事件就永远不会发生**。\n"
        "   下面这些发生了就**必须**往前推，不要等到「剧情感觉过了很久」：\n"
        "   赶路（开车/步行到另一个地点）· 等人等事（「等州警来」这类）· "
        "过夜或睡觉（换到下一天）· 长时间搜查一个大地方 · 玩家明说要等到某个时段。\n"
        "   🔴 **只能往前，不能倒退**（代码会拒掉倒流的写入）。同一时段内的小动作"
        "不用每轮改，但上面那几种发生了就得改。"
    ),
)

_OUTPUT_EXAMPLE = PromptBlock(
    slot="output_example",
    order=40,
    text='  "state_updates": [{"subject": "world", "key": "当前场景", "value": "书房"}]',
)

PROMPT_BLOCKS = (_RULE_STATE, _RULE_GAME_TIME, _OUTPUT_EXAMPLE)
