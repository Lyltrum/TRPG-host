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
    text='4c. **潜行/隐匿**：调查员藏起来、贴墙躲进阴影、跟踪时不想被发现——潜行检定成功（或情境本身足以藏住）就写 `hiding: [{"player": "昵称", "hidden": true}]`。隐匿的人**照常听得见**这里发生的一切，但同处的其他人不知道他在场。被发现、主动现身、离开这个地点时必须写回 `hidden: false`。局面块标了「（隐匿中）」的人，叙事里不要让别人看见他。',
)

_RULE_NEW_PLACE = PromptBlock(
    slot="rules",
    # 4d 已被 world_state 的「游戏内时间」占用（order 4.8）——编号是给模型读的
    # 顺序标签，撞号会让两条规则看起来是同一条的两半。
    order=4.9,
    text="""4e. **玩家去了剧本里没有的地方**：原文提过但没写成场景的地方（某个 NPC 的家、镇上的教堂、路边的墓地），玩家想去就让他去——**但要给它一个落点**：填 `new_location: {"name": "卡比家", "from_id": "当前节点 id"}`，系统会分配一个 `loc-N` 并把发言的人挪过去。
   🔴 **不要**把这种地方的名字直接写进 `current_node_id`，也不要拿一个 NPC id 顶替（真机出过：玩家说"去卡比家"，裁决器写了那个人的 id，位置当场作废）。**已经建过的地点在局面块「这一局即兴出来的地点」里列着，去那儿就直接用它的 `loc-N`，不要重复新建。**
   即兴地点上没有模组标注的检定点，检定按常识裁定即可。""",
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
    _EXAMPLE_NODE,
    _EXAMPLE_NEW_LOCATION,
    _EXAMPLE_MOVES,
    _EXAMPLE_HIDING,
)
