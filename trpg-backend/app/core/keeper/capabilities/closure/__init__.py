"""closure：没有预设结局时的自然收尾（`exec/30 §10.4`）。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `story_ran_its_course` 一个裁决字段 |
| `prompt.py` | 规则 10b + 一行输出示例 |
| `remaining.py` | 局面块：三个存量计数（缺数据显式降级，不报 0）+ 停滞轮数 |
| `executor.py` | 「去过的节点」与「无进展轮数」记账 + 反向门 + 收束到 `ending` |

🔴 **与 `progression` 的分工**：那片管「模组给的结局候选命中了」
（`ending_reached`），这片管「模组根本没给候选」。真人 KP 收尾做两件事——
判断"没有更多内容了" + 主动制造一个停得住的地方；agent 此前只有前者的一半，
**没有"生产"这个动作**，所以开放式模组不是判断失败，是根本没有动作可做。

🔴 **它是局面块 + 反向门，不是正向门**：不判断"故事是不是真的完了"（语义，
代码做不了），只在明显还没完时拦住。

🔴 **2026-08-12 修正：这片能力自己也犯过同一个错。** 局面块的三个数本来是
**参考材料**，规则 10b 却把它们写成了**准入门槛**（"三个数都见底才准收"）
——代码替 KP 做了它本来就该做的判断。真人反馈实证了它的另一头：玩家一直
偏离主线，三个数永远不见底，于是**永远等不到落幕**。

修法不是调阈值，是**让判错的代价变小**：收尾落在 `ending`（可撤回），玩家
接着说话就自动退回 `investigation`。代价小了，判断权才敢真正交回去。
配套加了「无进展轮数」——前三份记账全是存量，回答不了"是不是在原地打转"，
而真人 KP 判断收尾时数的正是后者。
"""

from app.core.keeper.capabilities.closure.executor import execute_closure
from app.core.keeper.capabilities.closure.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.closure.remaining import render_remaining_content
from app.core.keeper.capabilities.closure.schema import (
    FIELD_CAPABILITIES,
    ClosureDecisionFields,
    audit_fields,
)
from app.core.keeper.contract.registry import ExecutorHook, KeeperCapability, SituationBlock
from app.core.keeper.runtime.progress_state import STALLED_TURNS_KEY, VISITED_NODES_KEY

CAPABILITY = KeeperCapability(
    name="closure",
    schema=ClosureDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    # 85 > progression 的 80：剧本自己的结局先判，没命中才轮到自然收尾。
    executors=(ExecutorHook(order=85, run=execute_closure),),
    situations=(
        SituationBlock(
            order=41,
            heading="这一局走到哪了（参考材料，不是收尾的门槛）",
            render=render_remaining_content,
        ),
    ),
    audit=audit_fields,
    reserved_state_keys=(VISITED_NODES_KEY, STALLED_TURNS_KEY),
)
