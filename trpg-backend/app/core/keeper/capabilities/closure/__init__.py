"""closure：没有预设结局时的自然收尾（`exec/30 §10.4`）。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `story_ran_its_course` 一个裁决字段 |
| `prompt.py` | 规则 10b + 一行输出示例 |
| `remaining.py` | 局面块：两行门槛 + 两行参考（缺数据显式降级，不报 0）、核心真相清单 |
| `executor.py` | 「去过的节点」与「无进展轮数」记账 + 反向门 + 收束到 `ending` |

🔴 **与 `progression` 的分工**：那片管「模组给的结局候选命中了」
（`ending_reached`），这片管「模组根本没给候选」。真人 KP 收尾做两件事——
判断"没有更多内容了" + 主动制造一个停得住的地方；agent 此前只有前者的一半，
**没有"生产"这个动作**，所以开放式模组不是判断失败，是根本没有动作可做。

🔴 **它是局面块 + 反向门，不是正向门**：不判断"故事是不是真的完了"（语义，
代码做不了），只在明显还没完时拦住。

## 🔴 2026-08-13：真人反馈「永远等不到落幕」的真根因是代码 bug

原来的门要求「三个数都见底」，而其中「没去过的地方」数的是扁平展开的**全部
节点**，玩家位置却只落在**地点类**节点上（林中屋 23 : 14）——那个数**永远
到不了 0**，门在结构上不可能通过。

当天上午我的修法是**把门整个拆掉**（"你自己判断，那些数只是参考"），并顺手
把「无进展轮数」也写成了收尾依据。两处都被推翻：

- 拆门是**绕过 bug**。门没错，错的是它数错了东西 ⇒ 修数不修门。
- **「卡住了」和「做完了」是相反的处境**，不能共用一个信号：打转该**推**
  （给线索 / 让事件闯进来），内容跑完了才该**收**。

现在：门只留有 id、有记账、分母到得了底的两样（未揭开配对全部归零、一次性
议程全部触发，**不设比例阈值**——阈值一旦是拍的就永远调不完）；「没去过的
地方」与「无进展轮数」降级为局面块里的参考，文本里明写它们不是依据。

**可撤回的中间态保留**：收尾落在 `ending`，玩家接着说话就自动退回
`investigation`（见 `agent.py`）。它管的是"提议错了怎么办"，跟"何时提议"
是两件事，不因为门回来了就撤掉。
"""

from app.core.keeper.capabilities.closure.executor import execute_closure
from app.core.keeper.capabilities.closure.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.closure.remaining import (
    render_key_facts,
    render_remaining_content,
)
from app.core.keeper.capabilities.closure.schema import (
    FIELD_CAPABILITIES,
    ClosureDecisionFields,
    audit_fields,
)
from app.core.keeper.contract.registry import ExecutorHook, KeeperCapability, SituationBlock
from app.core.keeper.runtime.progress_state import (
    PROGRESS_SOURCE_KEY,
    STALLED_TURNS_KEY,
    VISITED_NODES_KEY,
)

CAPABILITY = KeeperCapability(
    name="closure",
    schema=ClosureDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    # 85 > progression 的 80：剧本自己的结局先判，没命中才轮到自然收尾。
    executors=(ExecutorHook(order=85, run=execute_closure),),
    situations=(
        # 核心真相排在存量前面：真人 KP 判"该收了"先看真相揭开没有，再看还剩
        # 多少格子。`keeper_only`——它是 kp_truth，叙事器那份不给。
        SituationBlock(
            order=40.5,
            heading="这份模组的核心真相（绝密，收尾判断的参照）",
            render=render_key_facts,
            keeper_only=True,
        ),
        SituationBlock(
            order=41,
            heading="这一局还剩多少内容",
            render=render_remaining_content,
        ),
    ),
    audit=audit_fields,
    reserved_state_keys=(VISITED_NODES_KEY, STALLED_TURNS_KEY, PROGRESS_SOURCE_KEY),
)
