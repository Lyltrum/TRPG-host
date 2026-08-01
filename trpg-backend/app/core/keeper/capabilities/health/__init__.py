"""health：生命值变化（调查员与 NPC）。

新人只读这一个目录就能改这个能力：

| 文件 | 管什么 |
|---|---|
| `schema.py` | 裁决器**能说什么**（`hp_changes` 字段片段） |
| `prompt.py` | 裁决器**什么时候说**（规则 3b + 输出示例那一行） |
| `executor.py` | 说了之后**世界怎么变**（角色卡 / NPC 状态表两条记账） |
| `schema.audit_fields` | 本轮动没动手，进 `keeper_decision` 日志 |
| `npc_state.py` | NPC 血量的存储形态与寻址（白名单 npc id） |
| `test_health_capability.py` | 上面四件事各自的验收 |

原名叫 `combat` 被否掉了：这里只管血量增减，不管回合顺序、先攻、命中——
名字比内容大会误导人来这里找不存在的东西（`exec/27` 三处撞名一起理顺）。
"""

from app.core.keeper.capabilities.health.executor import execute_hp_changes
from app.core.keeper.capabilities.health.npc_state import NPC_STATE_KEY, format_npc_states
from app.core.keeper.capabilities.health.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.health.schema import (
    FIELD_CAPABILITIES,
    HealthDecisionFields,
    audit_fields,
)
from app.core.keeper.registry import ExecutorHook, KeeperCapability, SituationBlock

CAPABILITY = KeeperCapability(
    name="health",
    schema=HealthDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    # HP 变更排在所有副作用最前（与切分前的执行顺序一致——执行报告的行序
    # 会原样喂给叙事阶段，顺序变了叙事读到的"发生了什么"就变了）。
    executors=(ExecutorHook(order=10, run=execute_hp_changes),),
    # 局面块里紧跟在「各自所在」之后。有记录时它是**权威值**——裁决器不该
    # 再从上一段散文里猜"它伤到什么程度"（exec/19 #39）。
    situations=(
        SituationBlock(
            order=20,
            heading="NPC 当前状态（对局内实时值，优先于剧本数据卡）",
            render=format_npc_states,
        ),
    ),
    audit=audit_fields,
    # 🔴 补登记（阶段 3 复查发现）：此前两张清单都没有它，模型一条
    # state_updates 就能把 NPC 血量记录覆盖成字符串、记账静默清零。
    reserved_state_keys=(NPC_STATE_KEY,),
)
