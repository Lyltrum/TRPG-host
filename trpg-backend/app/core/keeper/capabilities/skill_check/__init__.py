"""skill_check：技能/属性检定（含对抗与模组护栏）。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `CheckRequest` / `OpposedTarget` + `checks` 字段片段 |
| `prompt.py` | 规则 1 / 1b / 1c / 2 / 7 + 输出示例那一行 |
| `executor.py` | 技能值解析、待掷解析、掷骰与对抗结算 |
| `guard.py` | 模组标注护栏（设计 02）——只放行剧本标注过的检定点 |

共用的东西全部下沉：掷骰与成功等级在 `primitives/dice.py`、技能 id 白名单在
`primitives/skills.py`、两段式待掷队列在 `keeper/pending.py`。留在这里的只有
"这个行动该不该掷、拿哪个值掷、对手怎么比"。
"""

from app.core.keeper.capabilities.skill_check.executor import (
    apply_skill_check,
    create_pending_skill_checks,
    publish_stealth_check_requests,
    settle_skill_check,
)
from app.core.keeper.capabilities.skill_check.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.skill_check.schema import (
    FIELD_CAPABILITIES,
    SkillCheckDecisionFields,
    audit_fields,
)
from app.core.keeper.contract.registry import (
    ExecutorHook,
    KeeperCapability,
    PendingHook,
    SettleHook,
)

CAPABILITY = KeeperCapability(
    name="skill_check",
    schema=SkillCheckDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    # 🔴 order=5：这个执行钩子**什么都不改**，只把"本轮谁要掷潜行"发布到
    # `TurnFacts`，好让 `movement`（order=30）把"进入隐匿"让给检定结算。
    # 必须排在 movement 前面，也必须在执行阶段——待掷记录的创建整个排在
    # 执行之后，等不到。
    executors=(ExecutorHook(order=5, run=publish_stealth_check_requests),),
    # 排在 san_check 之前：待掷队列的顺序就是玩家看到卡片的顺序，与切分前
    # create_pending_checks 里"先 checks 后 san_checks"一致。
    pendings=(PendingHook(order=10, run=create_pending_skill_checks),),
    settlers=(SettleHook(kind="skill", run=settle_skill_check, apply=apply_skill_check),),
    audit=audit_fields,
)
