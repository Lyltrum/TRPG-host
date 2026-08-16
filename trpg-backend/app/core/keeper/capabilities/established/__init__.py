"""established：既成事实（已经了结、但后果永久为真的即兴事实的落点）。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `new_facts`（只给文本；**故意没有结清动作**） |
| `prompt.py` | 规则 4h + 一行输出示例 |
| `executor.py` | 分配 id、只增不删 |
| `state.py` | 存储形态 + 局面块「已经成为定局的事」 |

🔴 **跟 `open_threads` 不是一回事**（会被当成重复，所以写在最前面）：
两者生命周期**正好相反**——悬而未决是"还没了结、需要你继续演"，有
`resolved_threads`；既成事实是"已经了结、后果永远为真"，**没有**结清动作。
把烧掉的木屋塞进 threads 的话，模型迟早会把它标成已解决（那边是 `pop`），
那条记忆当场蒸发。

🔴 **代码已经记账的东西不归这里**：NPC 死没死走 `health` 的 `NPC状态`、
物品走 `inventory`、谁在场走 `presence`/`cast`、线索走事实账本 L1。
那几样本来就是持久化且每轮渲染的——再记一份正是 `exec/40` ④ 刚拦掉的
「两份账」。这一片只收**没有任何能力认领**的那些。

形态照抄即兴地点（`exec/32`）与悬而未决（`exec/36`）：模型给文本、代码分配
id、局面块全量列出。**第三个实例，所以是照抄不是新造。**
"""

from app.core.keeper.capabilities.established.executor import execute_established
from app.core.keeper.capabilities.established.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.established.schema import (
    FIELD_CAPABILITIES,
    EstablishedDecisionFields,
    audit_fields,
)
from app.core.keeper.capabilities.established.state import (
    ESTABLISHED_KEY,
    ESTABLISHED_SEQ_KEY,
    format_established,
)
from app.core.keeper.contract.registry import ExecutorHook, KeeperCapability, SituationBlock

CAPABILITY = KeeperCapability(
    name="established",
    schema=EstablishedDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    # 紧跟 open_threads（55）之后：本轮先关掉了结的处境，再记下成为定局的事。
    executors=(ExecutorHook(order=56, run=execute_established),),
    # 摆在「悬而未决的事」（30）之后：两块并排读，"还没完的"与"已经定了的"
    # 一眼分得开。
    situations=(SituationBlock(order=32, heading="已经成为定局的事", render=format_established),),
    audit=audit_fields,
    reserved_state_keys=(ESTABLISHED_KEY, ESTABLISHED_SEQ_KEY),
)
