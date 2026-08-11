"""luck_spend：花幸运把一次失败推成成功（`exec/26 #66`，`exec/34` 第 4 步）。

| 文件 | 管什么 |
|---|---|
| `rules.py` | 规则边界 + 硬门（阈值常量在这里） |
| `executor.py` | 要不要问（offer）· 答完怎么算（resolve） |

## 为什么它是第九个钩子

前八个覆盖"裁决→执行"和"发起→结算"两种形状。这一片是第三种：**结算之后还要
再等玩家一拍**。`PendingFn` 的输入是裁决，接不住它；`SettleHook` 方向相反。
`exec/26 #66` 预言了这个钩子，`exec/34` 把它落地成 `PostSettleHook`。

🔴 **它一个 schema 字段都不需要**：offer 发不发是纯代码判定，裁决器对这件事
一个字都不用说。概率面为零。（我曾说这个功能"能压满八个钩子"——那是照着钩子表
凑功能，没照着功能想钩子。）

## 本期没做的两半，以及为什么

- **老手主动索取**（`#66` 里那条"不受阈值限制、从检定记录发起"）：它要么发生在
  同一个窗口里（那就是现在这张卡），要么就是对一次**已经生效**的检定回滚记账、
  解隐匿、已经说出去的叙事——**说出去的话收不回来**。所以它不是"再加个按钮"，
  是另一个设计问题。
- **KP 在叙事里多说一句引导**：那句话该出现在卡片弹出的**同时**，而那一刻按设计
  根本没有叙事发生（叙事在决定之后）。卡片自己就是教学位——写清差几点、花多少、
  剩多少、以及花了也可能还是输。

## 两条频率去重也没做

`#66` 提的「同一条线索只问一次」要新开一个 `keeper_state` 键；「幸运余额低于
20% 不再主动弹」**算不出来**——COC7 的幸运没有"上限"这个值，要么再造一份真相，
要么拍一个常数。两条都留在 `#66` 里，不在这里静默塞一个假分母。
"""

from app.core.keeper.capabilities.luck_spend.executor import offer_luck_spend, resolve_luck_spend
from app.core.keeper.contract.registry import KeeperCapability, PostSettleHook
from app.core.keeper.runtime.pending import LUCK_SPEND_KIND

CAPABILITY = KeeperCapability(
    name="luck_spend",
    post_settles=(
        PostSettleHook(
            kind=LUCK_SPEND_KIND,
            order=10,
            offer=offer_luck_spend,
            resolve=resolve_luck_spend,
        ),
    ),
)
