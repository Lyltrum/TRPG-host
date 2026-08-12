"""presence：谁刚到、谁刚走（中途加入与中途离开的剧情落点）。

| 文件 | 管什么 |
|---|---|
| `state.py` | 「已交代登场」集合 + 「待交代离场」名单 + 局面块 |
| `executor.py` | 记账（本轮摆到模型眼前了就算交代过） |

🔴 **这一片没有裁决字段。** 它不需要模型"说"什么，它需要模型在叙事里**写**
一句把人圆进去/圆出去。所以只有 situation + executor 两个钩子——注册表本来
就允许一片能力只用得上其中几个。

结构性与纪律性在这里分得很清，别混：
- **暂离的人不进在场名单**（`agent._load_room_memory` 按 `Player.away` 过滤）
  ——这一半是**硬的**，模型想提他也提不了，属于「保密靠拿不到」同族。
- **把登场/离场写得好听**——这一半是**概率性**的，登记在 `exec/20`。
"""

from app.core.keeper.capabilities.presence.executor import mark_presence_announced
from app.core.keeper.capabilities.presence.state import (
    ANNOUNCED_ARRIVALS_KEY,
    PENDING_DEPARTURES_KEY,
    render_presence,
)
from app.core.keeper.contract.registry import ExecutorHook, KeeperCapability, SituationBlock

CAPABILITY = KeeperCapability(
    name="presence",
    # 排在最后（90）：记账的判据是"本轮局面块已经摆到模型眼前了"，那要等这一轮
    # 别的能力都执行完——中途有人被移动/被判离场时，名单以最终状态为准。
    executors=(ExecutorHook(order=90, run=mark_presence_announced),),
    # 排在「各自所在」（10）之前：先知道桌上有谁变了，再看谁在哪。
    situations=(SituationBlock(order=8, heading="桌上的人变了", render=render_presence),),
    reserved_state_keys=(ANNOUNCED_ARRIVALS_KEY, PENDING_DEPARTURES_KEY),
)
