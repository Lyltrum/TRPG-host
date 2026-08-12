"""madness：临时性疯狂（有 id 的状态）。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `madness_recovered` 字段片段——**只有解除，没有进入** |
| `prompt.py` | 规则 3c + 输出示例那一行 |
| `executor.py` | 解除：把裁决落成真正的解除，没在疯的人记 issue |
| `situation.py` | 局面块「疯狂中的调查员」：谁在疯 · 哪一种 · 怎么演 |

🔴 **进入疯狂不在这里**，在 `capabilities/san_check` 的理智损失落卡那一步：
触发条件（单次损失 ≥5）是它算出来的数。两片能力不许互相 import，所以状态
本身落在共同的下游 `runtime/madness_state.py`——判据同 `movement` 与
`location_state`：共享的状态与读写归 runtime，裁决字段与执行归能力。

症状表（1D10）在 `RulesetRead.madness_symptoms`：规则系统是插件，那张表是
COC7 的知识不是引擎的。这套规则没有那张表 ⇒ 没有人会进入疯狂、局面块整块
不渲染，**不伪造一个默认症状**。
"""

from app.core.keeper.capabilities.madness.executor import execute_madness_recovered
from app.core.keeper.capabilities.madness.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.madness.schema import (
    FIELD_CAPABILITIES,
    MadnessDecisionFields,
    audit_fields,
)
from app.core.keeper.capabilities.madness.situation import format_madness
from app.core.keeper.contract.registry import ExecutorHook, KeeperCapability, SituationBlock
from app.core.keeper.runtime.madness_state import MADNESS_KEY

CAPABILITY = KeeperCapability(
    name="madness",
    schema=MadnessDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    # 排在 san_check（40）之后：本轮理智检定造成的发作要先落库，同一轮里
    # "刚发疯又立刻好了"才不至于被解除先执行掉。
    executors=(ExecutorHook(order=50, run=execute_madness_recovered),),
    # 紧跟在「NPC 当前状态」（20）之后、即兴地点之前：它是关于**人**的状态，
    # 该跟位置/血量摆在一起，而不是垫在剧本数据后面。
    situations=(SituationBlock(order=25, heading="疯狂中的调查员", render=format_madness),),
    audit=audit_fields,
    reserved_state_keys=(MADNESS_KEY,),
)
