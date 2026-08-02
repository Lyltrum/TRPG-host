"""clue_reveal：线索揭示（原 `visibility`，exec/27 三处撞名一起理顺）。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `clues_revealed` 字段片段 + 权限 + 留痕 |
| `prompt.py` | 规则 9 + 输出示例那一行 |
| `executor.py` | pair id 白名单校验 + `mark_clues_revealed_impl` |
| `pairs.py` | 「哪条配对已对谁揭开」的存储形态与局面块渲染 |

改了什么、没改什么：代码里的 `visibility` → `clue_reveal`、
`visibility_revealed` → `clues_revealed`；**`ScenarioModule.visibility_pairs`
与 `keeper_state` 里的键值 `"已揭开配对"` 不动**（前者要迁移五个模组，后者会让
在跑的房间读不到自己的记录）。
"""

from app.core.keeper.capabilities.clue_reveal.executor import execute_clues_revealed
from app.core.keeper.capabilities.clue_reveal.pairs import CLUES_REVEALED_KEY, render_clue_status
from app.core.keeper.capabilities.clue_reveal.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.clue_reveal.schema import (
    FIELD_CAPABILITIES,
    ClueRevealDecisionFields,
    audit_fields,
)
from app.core.keeper.registry import ExecutorHook, KeeperCapability, SituationBlock

CAPABILITY = KeeperCapability(
    name="clue_reveal",
    schema=ClueRevealDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    executors=(ExecutorHook(order=70, run=execute_clues_revealed),),
    situations=(SituationBlock(order=60, heading="密级配对状态", render=render_clue_status),),
    audit=audit_fields,
    reserved_state_keys=(CLUES_REVEALED_KEY,),
)
