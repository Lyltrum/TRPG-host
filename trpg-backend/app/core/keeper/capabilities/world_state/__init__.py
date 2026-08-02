"""world_state：裁决器记的自由文本世界状态（跨轮记忆的唯一来源）。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `StateUpdate`（带 subject）+ `state_updates` 字段片段 |
| `prompt.py` | 规则 4 + 规则 4d（游戏内时间）+ 输出示例那一行 |
| `executor.py` | 主体白名单解析 + 保留键闸门 + 落库 |

🔴 **本能力不声明 `reserved_state_keys`**：它写的就是自由文本键，没有"属于
自己"的那种由代码记账的键。它反过来是**闸门的消费者**——`deps.reserved_state_keys`
里是别人的键，它一个都不许碰。
"""

from app.core.keeper.capabilities.world_state.executor import execute_state_updates
from app.core.keeper.capabilities.world_state.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.world_state.schema import (
    FIELD_CAPABILITIES,
    WorldStateDecisionFields,
    audit_fields,
)
from app.core.keeper.contract.registry import ExecutorHook, KeeperCapability

CAPABILITY = KeeperCapability(
    name="world_state",
    schema=WorldStateDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    executors=(ExecutorHook(order=20, run=execute_state_updates),),
    audit=audit_fields,
)
