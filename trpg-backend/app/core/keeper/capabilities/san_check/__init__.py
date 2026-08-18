"""san_check：理智检定。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `SanCheckRequest` + `san_checks` 字段片段 |
| `prompt.py` | 规则 3（理智）+ 输出示例那一行 |
| `executor.py` | 待掷解析 + 掷骰结算（写角色卡 SAN）+ 检定点已触发记账 |
| `state.py` | 模组标注的理智检定点：局面块 + 已触发记账的存储形态 |

🔴 **跟 `skill_check` 是两片，不是一片。** 它们共用的东西全部下沉了：
掷骰与成功等级在 `primitives/dice.py`、两段式待掷队列在 `keeper/pending.py`。
留在各自能力里的只有"这类检定怎么算"。合成一片叫 `checks` 的话，那个名字会
同时装下共享机制与能力知识——用户凭手感说"太大"，架构测试的「能力之间不许
互相 import」会在代码里抓到同一件事（exec/27）。
"""

from app.core.keeper.capabilities.san_check.executor import (
    apply_san_check,
    create_pending_san_checks,
    mark_san_points_fired,
    settle_san_check,
)
from app.core.keeper.capabilities.san_check.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.san_check.schema import (
    FIELD_CAPABILITIES,
    SanCheckDecisionFields,
    audit_fields,
)
from app.core.keeper.capabilities.san_check.state import (
    RECENT_SAN_KEY,
    SAN_POINTS_FIRED_KEY,
    render_recent_san,
    render_san_points,
)
from app.core.keeper.contract.registry import (
    ExecutorHook,
    KeeperCapability,
    PendingHook,
    SettleHook,
    SituationBlock,
)

CAPABILITY = KeeperCapability(
    name="san_check",
    schema=SanCheckDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    # 排在 skill_check 之后：待掷队列的顺序就是玩家看到卡片的顺序，
    # 与切分前 create_pending_checks 里"先 checks 后 san_checks"一致。
    pendings=(PendingHook(order=20, run=create_pending_san_checks),),
    settlers=(SettleHook(kind="san", run=settle_san_check, apply=apply_san_check),),
    # 排在 movement（30）之后：记账要用本轮移动完成后的位置。
    executors=(ExecutorHook(order=40, run=mark_san_points_fired),),
    # 🔴 `keeper_only`：这块整段都是**写给裁决器的指令**（"必须在 `san_checks`
    # 里发起、损失表达式照抄下面的数值"），叙事器读到它就等于把 `0/1D6` 摆在
    # 它眼前——真机上它直接念给了玩家听（`exec/23 #77`）。
    situations=(
        SituationBlock(order=45, heading="理智检定点", render=render_san_points, keeper_only=True),
        # 紧跟在检定点后面：两块一起读才完整——那块说"还有哪些没掷"，
        # 这块说"刚为什么掷过"。同样 `keeper_only`：这是写给裁决器的纪律，
        # 叙事器读到"最近掷过什么"会把它当成可以复述的剧情。
        SituationBlock(
            order=46, heading="最近的理智检定", render=render_recent_san, keeper_only=True
        ),
    ),
    audit=audit_fields,
    reserved_state_keys=(SAN_POINTS_FIRED_KEY, RECENT_SAN_KEY),
)
