"""san_check：理智检定。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `SanCheckRequest` + `san_checks` 字段片段 |
| `prompt.py` | 规则 3（理智）+ 输出示例那一行 |
| `executor.py` | 待掷解析 + 掷骰结算（写角色卡 SAN） |

🔴 **跟 `skill_check` 是两片，不是一片。** 它们共用的东西全部下沉了：
掷骰与成功等级在 `primitives/dice.py`、两段式待掷队列在 `keeper/pending.py`。
留在各自能力里的只有"这类检定怎么算"。合成一片叫 `checks` 的话，那个名字会
同时装下共享机制与能力知识——用户凭手感说"太大"，架构测试的「能力之间不许
互相 import」会在代码里抓到同一件事（exec/27）。
"""

from app.core.keeper.capabilities.san_check.executor import (
    create_pending_san_checks,
    settle_san_check,
)
from app.core.keeper.capabilities.san_check.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.san_check.schema import (
    FIELD_CAPABILITIES,
    SanCheckDecisionFields,
    audit_fields,
)
from app.core.keeper.contract.registry import KeeperCapability, PendingHook, SettleHook

CAPABILITY = KeeperCapability(
    name="san_check",
    schema=SanCheckDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    # 排在 skill_check 之后：待掷队列的顺序就是玩家看到卡片的顺序，
    # 与切分前 create_pending_checks 里"先 checks 后 san_checks"一致。
    pendings=(PendingHook(order=20, run=create_pending_san_checks),),
    settlers=(SettleHook(kind="san", run=settle_san_check),),
    audit=audit_fields,
)
