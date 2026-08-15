"""inventory：随身物品的增减（`exec/38`，2026-08-14 实测）。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `equipment_changes` 字段（增量）+ 权限 + 留痕 |
| `prompt.py` | 规则 3c + 输出示例那一行 |
| `executor.py` | 落到角色卡的 `equipment` 列上 |

🔴 **起因**：叙事写「州警扣下扳机」而玩家的随身是空的——那一枪从头到尾没有
出处。查下来整条链只差一半：建卡第 6 步早就有「装备与物品」那一栏，
`sheet_digest` 也早就把「随身：…」渲进了裁决局面块（注释原话：**有没有光源、
有没有枪，直接决定一段叙事成不成立**），**唯独缺"剧情里拿到的东西怎么进来"**。

🔴 **没有局面块**：随身清单已经在角色卡那一段里了（`format_party_sheet`），
再渲一块就是同一份数据两个出口。加一片能力**不等于**每个钩子都要挂。
"""

from app.core.keeper.capabilities.inventory.executor import execute_equipment_changes
from app.core.keeper.capabilities.inventory.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.inventory.schema import (
    FIELD_CAPABILITIES,
    InventoryDecisionFields,
    audit_fields,
)
from app.core.keeper.contract.registry import ExecutorHook, KeeperCapability

CAPABILITY = KeeperCapability(
    name="inventory",
    schema=InventoryDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    # 排在 health（20）之后：同一拍里"被打伤 + 东西被夺走"是常见形状，
    # 两者互不依赖，挨着放只是让执行报告读起来是一件事。
    executors=(ExecutorHook(order=22, run=execute_equipment_changes),),
    audit=audit_fields,
)
