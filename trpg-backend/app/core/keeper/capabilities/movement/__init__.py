"""movement：位置 · 分头 · 隐匿。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `current_node_id` / `moves` / `hiding` 三个裁决字段 |
| `prompt.py` | 规则 4b（分头）+ 4c（潜行）+ 三行输出示例 |
| `executor.py` | 场景指针 → 分头移动 → 隐匿，顺序有语义 |

🔴 **位置状态本身不在这里**，在 `keeper/location_state.py`：叙事分组、讨论区
投递、检定护栏都在读它。判据同 `phase.py`——共享的状态与读写归 runtime，用它
做裁决的字段与执行归能力。

改名（exec/27 三处撞名之三）：`decision.stealth` → `hiding`。`stealth` 同时是
COC7 规则表里的**技能 id**，而技能 id 是规则权威、不能改。
"""

from app.core.keeper.capabilities.movement.executor import execute_movement
from app.core.keeper.capabilities.movement.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.movement.schema import (
    FIELD_CAPABILITIES,
    MovementDecisionFields,
    audit_fields,
)
from app.core.keeper.capabilities.movement.situation import render_party_locations
from app.core.keeper.location_state import HIDDEN_PLAYERS_KEY, PLAYER_LOCATION_KEY
from app.core.keeper.registry import ExecutorHook, KeeperCapability, SituationBlock
from app.core.keeper.scene_state import CURRENT_NODE_KEY

CAPABILITY = KeeperCapability(
    name="movement",
    schema=MovementDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    executors=(ExecutorHook(order=30, run=execute_movement),),
    situations=(
        SituationBlock(
            order=10,
            heading=(
                "各自所在（不在同一处的调查员看不见对方那边发生的事；标「隐匿中」的人"
                "听得见但别人看不见他）"
            ),
            render=render_party_locations,
        ),
    ),
    audit=audit_fields,
    reserved_state_keys=(CURRENT_NODE_KEY, PLAYER_LOCATION_KEY, HIDDEN_PLAYERS_KEY),
)
