"""agenda：议程时间轴——世界自己的时间表，不依赖玩家在场。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `agenda_fired` 字段片段 + 权限 + 留痕 |
| `prompt.py` | 规则 8 + 输出示例那一行 |
| `executor.py` | id 校验 + `mark_agenda_fired_impl`（once 幂等在这里） |
| `state.py` | 已触发列表的存储形态与局面块渲染 |
"""

from app.core.keeper.capabilities.agenda.executor import execute_agenda_fired
from app.core.keeper.capabilities.agenda.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.agenda.schema import (
    FIELD_CAPABILITIES,
    AgendaDecisionFields,
    audit_fields,
)
from app.core.keeper.capabilities.agenda.state import AGENDA_FIRED_KEY, render_agenda_status
from app.core.keeper.contract.registry import ExecutorHook, KeeperCapability, SituationBlock

CAPABILITY = KeeperCapability(
    name="agenda",
    schema=AgendaDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    executors=(ExecutorHook(order=60, run=execute_agenda_fired),),
    situations=(SituationBlock(order=50, heading="议程状态", render=render_agenda_status),),
    audit=audit_fields,
    reserved_state_keys=(AGENDA_FIRED_KEY,),
)
