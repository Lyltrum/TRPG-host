"""recall：玩家问过去的事时，把原文从历史里查回来（`exec/47` P2）。

| 文件 | 管什么 |
|---|---|
| `schema.py` | `recall_query`（检索词，绝大多数拍为 None） |
| `prompt.py` | 规则 4i + 输出示例 |

🔴 **这一片没有 executor，也没有 situation 钩子**，因为它两样都不是：
它不改世界状态（没有副作用），而它要注入的那一段**必须查库**（异步），
而 `SituationBlock.render` 是同步的。召回发生在 `agent._narrate_per_audience`
里——那是唯一同时握着 **这一段的受众** 和 **一个 await 点** 的地方，
而受众不能少（分头时门厅那段召不回地下室的原文）。

检索本身在 `memory/recall.py`，跟 L3 同目录：它是同一份历史的另一种读法，
**而且刻意复用 `history_lines_from_events` + `visible_history`**，不另写一份。
"""

from app.core.keeper.capabilities.recall.prompt import PROMPT_BLOCKS
from app.core.keeper.capabilities.recall.schema import (
    FIELD_CAPABILITIES,
    RecallDecisionFields,
    audit_fields,
)
from app.core.keeper.contract.registry import KeeperCapability

CAPABILITY = KeeperCapability(
    name="recall",
    schema=RecallDecisionFields,
    field_capabilities=FIELD_CAPABILITIES,
    prompt_blocks=PROMPT_BLOCKS,
    audit=audit_fields,
)
