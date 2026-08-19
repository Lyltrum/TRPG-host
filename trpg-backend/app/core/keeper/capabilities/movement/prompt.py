"""movement 能力贡献给裁决 prompt 的文本块。

🔴 文本是成品原文，不再经过 f-string / format，花括号写单个。
"""

from app.core.keeper.contract.registry import PromptBlock

_RULE_SPLIT = PromptBlock(
    slot="rules",
    order=4.4,
    text="""4b. **分头探索**：current_node_id 只管**本轮发言的人共同去了哪**。有人**单独**去别处（"我去地窖看看，你们留在客厅"）时，把他写进 moves：`[{"player": "昵称", "node_id": "cellar"}]`；没发言的人位置不动，不要用 current_node_id 把他们隔空挪走。全队在一起时 moves 就是空数组。
   🔴 **`moves` 也是"把一个没发言的人带上"的唯一写法**：AI 队友不会自己宣告行动（它只在讨论区出主意），所以真人说「我和阿铁一起去地下室」时，阿铁不在"本轮发言的人"里、不会被 current_node_id 带走——**必须**同时写 `moves: [{"player": "阿铁", "node_id": "cellar"}]`，否则他会被留在原地。被点名带上的同伴照此办理。
   局面块出现「各自所在」小节时说明已经分头——**不在同一处的调查员看不见对方那边发生的事**，narration_guidance 要分别交代各处，不要让两边的人凭空知道对方的发现。""",
)

_RULE_HIDING = PromptBlock(
    slot="rules",
    order=4.6,
    text="""4c. **潜行/隐匿**：调查员藏起来、贴墙躲进阴影、跟踪时不想被发现——潜行检定成功（或情境本身足以藏住）就写 `hiding: [{"player": "昵称", "hidden": true}]`。隐匿的人**照常听得见**这里发生的一切，但同处的其他人不知道他在场。主动现身、离开这个地点时必须写回 `hidden: false`。局面块标了「（隐匿中）」的人，叙事里不要让别人看见他。
   🔴 **有人在找隐匿的人时，发一次「隐匿者本人的潜行对抗」**：`checks: [{"skill_id": "stealth", "player": "隐匿者昵称", "reason": "科比特举灯扫过屏风后面", "opposed": {"opponent": "科比特", "value": 他的侦察/聆听百分值}}]`。**输了系统会自动把他移出隐匿，你不用再写 `hiding`**。反过来写（让搜索者掷侦察、把隐匿者填进 opponent）系统接不住，隐匿会一直挂着。""",
)

_RULE_NEW_PLACE = PromptBlock(
    slot="rules",
    # 4d 已被 world_state 的「游戏内时间」占用（order 4.8）——编号是给模型读的
    # 顺序标签，撞号会让两条规则看起来是同一条的两半。
    order=4.9,
    text="""4e. **玩家去了剧本里没有的地方**：填 `new_location: {"name": "卡比家", "from_id": "他现在所在的地点 id"}`，系统会分配一个 `loc-N`。
   🔴 **这里说的"地方"是「谁看得见谁」的单位，不是地图上的地名。** 两拨人只要互相看不见、听不见，就是两个地方——哪怕在同一栋房子里。所以下面这些**都要建**：绕到屋后、留在门口望风、上二楼、退到街对面盯梢、断后守住走廊。**判据只有一条：如果那边发生的事这边不该知道，那就是两个地方。**
   🔴 **只有一部分人过去时，必须写 `movers`**：「我一个人绕到屋后，你们守着门口」→ `new_location: {"name": "科比特家屋后", "from_id": "loc-1", "movers": ["阿福"]}`，其他人留在原地。`movers` 留空才是全队一起过去。**漏写 movers = 你把所有人都挪走了**，分头当场失效。
   🔴 **不要**把这种地方的名字直接写进 `current_node_id`，也不要拿一个 NPC id 顶替（真机出过：玩家说"去卡比家"，裁决器写了那个人的 id，位置当场作废）。**已经建过的地点在局面块「这一局即兴出来的地点」里列着，去那儿就直接用它的 `loc-N`，不要重复新建。**
   从某个剧本节点派生出去的地方（`from_id` 指着它），检定与线索仍按那个节点的标注走；跟剧本完全无关的新地方按常识裁定。""",
)

_RULE_REFUSED_PLACE = PromptBlock(
    slot="rules",
    # 紧跟 4e：它俩是同一件事的两个方向，隔开会让模型只记住一半（08-14 的
    # 「失败也给」与「治疗回血」正是这样漏掉的）。
    order=4.92,
    text="""4e-2. **你不打算让他去的时候，也不许直接宣告"你去不了"**：玩家明确说了要去哪、要往哪边走，而剧本不支持那个方向（那儿没有内容 / 主线在别处 / 去了这局就散了）——**给判定，或者给代价，不要用世界设定一句话把他的选择取消掉。**
   三条走得通的，挑一条：
   ① **让他去**（默认）：那儿没有剧本内容就按 4e 建一个即兴地点，让他扑个空、自己回来；
   ② **拦在骰子上**：真要拦就发一次检定（划船离岸→驾驶：船舶；翻墙→攀爬；顶着风跑→体质），失败按 1d **给代价不是没收**——他仍然可以再试、可以换办法；
   ③ **让阻力有形状**：挡住他的是一个看得见、能被应对的东西（浪太大、门锁着、有人拦在前面），不是"你就是出不去"。
   🔴 **真机反例**（2026-08-18）：玩家说「我不去那条鬼船，往有灯光的岸划」，回答是「那是幻影，海流已经卷过去了，再划也出不去」——没有检定、没有代价，玩家连着两拍说同一件事而世界一动不动。**剧本是单行道不构成理由**：单行道要靠"别的路走不通"演出来，不是靠宣布玩家的选择无效。
   例外只有一种：物理上根本做不到的（"我飞过去"）。那按规则 2 办——说明白为什么，并且给一条走得通的。""",
)

_EXAMPLE_NODE = PromptBlock(
    slot="output_example", order=50, text='  "current_node_id": "some-node-id"'
)
_EXAMPLE_NEW_LOCATION = PromptBlock(slot="output_example", order=55, text='  "new_location": null')
_EXAMPLE_MOVES = PromptBlock(slot="output_example", order=60, text='  "moves": []')
_EXAMPLE_HIDING = PromptBlock(slot="output_example", order=70, text='  "hiding": []')

PROMPT_BLOCKS = (
    _RULE_SPLIT,
    _RULE_HIDING,
    _RULE_NEW_PLACE,
    _RULE_REFUSED_PLACE,
    _EXAMPLE_NODE,
    _EXAMPLE_NEW_LOCATION,
    _EXAMPLE_MOVES,
    _EXAMPLE_HIDING,
)
