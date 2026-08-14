"""cast：台上此刻有谁（叙事里的 NPC ↔ 模组名册的绑定）。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `npcs_on_stage` 字段（id 白名单）+ 权限 + 留痕 |
| `prompt.py` | 规则 4e + 输出示例那一行 |
| `executor.py` | id 校验 + 整份覆盖（快照语义） |
| `state.py` | 存储形态 + 局面块「此刻在场的 NPC」 |

🔴 **跟 `health` 的 `NPC状态` 不是一回事**（会被当成重复，所以写在最前面）：
那张表记的是**伤成什么样**，这里记的是**谁在台上**。一份数据扮演两个角色
必出结构性 bug。

🔴 **跟模组名册也不是一回事**：`render_full` 里的【登场 NPC】是"剧本里有谁"，
整局不变；这里是"此刻跟玩家在一处的是谁"，每轮都可能变。

实据见 `state.py` 的模块 docstring：真人实测里同一个 NPC（卡比）被当成两个人，
主持人让他指路去找他自己。**位置有 id、线索有 id、悬而未决有 id，唯独台上的
人没有**——这一片补的就是那个缺口，形态照抄前两者（第三个实例，所以是照抄
不是新造）。
"""

from app.core.keeper.capabilities.cast.executor import execute_cast
from app.core.keeper.capabilities.cast.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.cast.schema import (
    FIELD_CAPABILITIES,
    CastDecisionFields,
    audit_fields,
)
from app.core.keeper.capabilities.cast.state import ON_STAGE_KEY, render_on_stage
from app.core.keeper.contract.registry import ExecutorHook, KeeperCapability, SituationBlock

CAPABILITY = KeeperCapability(
    name="cast",
    schema=CastDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    # 排在 movement（30）之后：台上有谁取决于人在哪，位置先落定。
    executors=(ExecutorHook(order=35, run=execute_cast),),
    # 排在「NPC 当前状态」附近、「悬而未决」（30）之前：先知道台上是谁，
    # 再看他们伤成什么样、还有什么事悬着。
    situations=(SituationBlock(order=28, heading="此刻在场的 NPC", render=render_on_stage),),
    audit=audit_fields,
    reserved_state_keys=(ON_STAGE_KEY,),
)
