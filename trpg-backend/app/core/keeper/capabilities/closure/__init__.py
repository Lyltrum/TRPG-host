"""closure：没有预设结局时的自然收尾（`exec/30 §10.4`）。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `story_ran_its_course` 一个裁决字段 |
| `prompt.py` | 规则 10b + 一行输出示例 |
| `remaining.py` | 局面块「还剩多少内容」三个计数（缺数据显式降级，不报 0） |
| `executor.py` | 「去过的节点」记账 + 反向门 + 收束 |

🔴 **与 `progression` 的分工**：那片管「模组给的结局候选命中了」
（`ending_reached`），这片管「模组根本没给候选」。真人 KP 收尾做两件事——
判断"没有更多内容了" + 主动制造一个停得住的地方；agent 此前只有前者的一半，
**没有"生产"这个动作**，所以开放式模组不是判断失败，是根本没有动作可做。

🔴 **它是局面块 + 反向门，不是正向门**：不判断"故事是不是真的完了"（语义，
代码做不了），只在明显还没完时拦住。判错的代价不对称——该收没收可恢复，
不该收却收了不可撤回。
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
from app.core.keeper.runtime.progress_state import VISITED_NODES_KEY

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
            heading="还剩多少内容（三个数都见底才考虑 story_ran_its_course）",
            render=render_remaining_content,
        ),
    ),
    audit=audit_fields,
    reserved_state_keys=(VISITED_NODES_KEY,),
)
