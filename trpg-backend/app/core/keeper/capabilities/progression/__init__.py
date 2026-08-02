"""progression：对局阶段推进与结局收束。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `opening_complete` / `ending_reached` 两个裁决字段 |
| `prompt.py` | 规则 10 + 两行输出示例 |
| `executor.py` | 收束优先于开场完成，落到 `keeper_state` |
| `endings.py` | 把「可能的结局与触发条件」每轮摆到裁决器眼前 |

🔴 **阶段值本身不在这里**，在 `keeper/phase.py`：编排层到处在读它（心跳、
叙事长度、finished 之后拒收行动）。共享的流程机制归 runtime，用它做裁决的
字段与执行归能力——同 `pending` 的处理。理由写在 `phase.py` 的模块说明里。
"""

from app.core.keeper.capabilities.progression.endings import format_endings_status
from app.core.keeper.capabilities.progression.executor import execute_progression
from app.core.keeper.capabilities.progression.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.progression.schema import (
    FIELD_CAPABILITIES,
    ProgressionDecisionFields,
    audit_fields,
)
from app.core.keeper.contract.registry import ExecutorHook, KeeperCapability, SituationBlock
from app.core.keeper.runtime.phase import ENDING_ID_KEY, PHASE_KEY

CAPABILITY = KeeperCapability(
    name="progression",
    schema=ProgressionDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    executors=(ExecutorHook(order=80, run=execute_progression),),
    situations=(
        SituationBlock(
            order=40,
            heading=(
                "可能的结局（每轮判断触发条件是否已满足；满足才写 ending_reached，"
                "未满足必须为 null）"
            ),
            render=format_endings_status,
        ),
    ),
    audit=audit_fields,
    reserved_state_keys=(PHASE_KEY, ENDING_ID_KEY),
)
