"""open_threads：悬而未决的事（即兴出来的处境的落点）。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `new_threads`（只给文本）+ `resolved_threads`（id 白名单） |
| `prompt.py` | 规则 4f + 两行输出示例 |
| `executor.py` | 先关后开，一次写库；编造的 id 记 issue |
| `state.py` | 存储形态 + 局面块「悬而未决的事」 |

🔴 **跟 `world_state` 不是一回事**（会被当成重复，所以写在最前面）：
`state_updates` 要求 `subject` 取自剧本白名单，而且它是键值覆盖，没有"这件事
还在不在"的概念。即兴出来的处境两头都不合——没有剧本 id 可挂，也需要一个
**显式的结束**。缺了结束就是 `#46`（隐匿永不解除）的形状。

形态照抄即兴地点（`exec/32`）：模型给文本、代码分配 id、局面块全量列出、
关闭只能从列出的 id 里挑。**那是这套形态的第二个实例**，所以是照抄不是新造。
"""

from app.core.keeper.capabilities.open_threads.executor import execute_open_threads
from app.core.keeper.capabilities.open_threads.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.open_threads.schema import (
    FIELD_CAPABILITIES,
    OpenThreadsDecisionFields,
    audit_fields,
)
from app.core.keeper.capabilities.open_threads.state import (
    OPEN_THREADS_KEY,
    OPEN_THREADS_SEQ_KEY,
    render_open_threads,
)
from app.core.keeper.contract.registry import ExecutorHook, KeeperCapability, SituationBlock

CAPABILITY = KeeperCapability(
    name="open_threads",
    schema=OpenThreadsDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    # 排在 madness（50）之后、agenda（60）之前：本轮该关的先关掉，议程再据当前
    # 局面判断触发——不然刚了结的威胁还挂在表上，影响它读到的处境。
    executors=(ExecutorHook(order=55, run=execute_open_threads),),
    # 摆在「疯狂中的调查员」（25）之后、「还剩多少内容」（40）之前：它跟血量、
    # 疯狂一样是"此刻的处境"，不是剧本数据。
    situations=(SituationBlock(order=30, heading="悬而未决的事", render=render_open_threads),),
    audit=audit_fields,
    # 序号也要保留：它不是"给模型看的世界状态"，而且模型一条 state_updates
    # 把它改小，id 就会开始复用（那正是这一片唯一一次变红抓到的东西）。
    reserved_state_keys=(OPEN_THREADS_KEY, OPEN_THREADS_SEQ_KEY),
)
