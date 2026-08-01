"""这一轮守秘人被允许动用哪些能力（exec/27 阶段 3 · B 族）。

## 它替掉了什么

此前 `agent.py` 里有四处这样的代码：

```python
decision = decision.model_copy(update={"checks": [], "san_checks": [], "moves": []})
```

看着像"清几个字段"，实际是编排层在行使一种权力：**这一轮禁止哪些能力生效**
（心跳轮不许发检定、玩家在提问所以世界不推进）。写死成字段名有两个后果：

1. 加一片能力时，"迷茫轮该不该清它"没有任何地方会问你；
2. 那几个字段名把 `agent.py` 焊在具体能力上，能力切不出去。

## 🔴 正解不是再加一个钩子

`exec/14 P2` 早就铺好了这条路：主体持有能力集，`build_decision_model` 让越权
动作**无法表达**，`sanitize_decision` 在执行边界清回默认。所谓"心跳轮不许发
检定"，本质就是**这一轮的主体持有一个更窄的能力集**——同一件事，不需要第二套
机制。所以这里只做一件事：把"本轮撤销哪些能力"翻译成一次 `sanitize`。

## 一个必须说清的粒度

`asks_kp`（玩家在向守秘人提问）要收走"移动"和"设置场景指针"，但**不收"隐匿"**
——藏没藏起来是已经成立的状态，不因为有人问了句话就现身。所以
`Capability.SET_HIDING` 与 `SET_SCENE` 是分开的两条，而不是像最初那样共用一条。
这个拆分是被 B 族逼出来的：合在一起就没法在不改行为的前提下做这次替换。
"""

from __future__ import annotations

from app.core.keeper.decision import KeeperDecision
from app.core.keeper.registry import Capability
from app.core.keeper.subject import ALL_CAPABILITIES, Subject, sanitize_decision

#: 发起检定的两条。心跳/开场/迷茫/怪话/提问都会收走它们。
CHECK_CAPABILITIES = frozenset({Capability.REQUEST_CHECK, Capability.REQUEST_SAN_CHECK})

#: 推进世界的空间手段：走到哪、谁单独去了别处。**不含隐匿**（见模块说明）。
SCENE_ADVANCE_CAPABILITIES = frozenset({Capability.SET_SCENE})


def revoke(decision: KeeperDecision, revoked: frozenset[Capability]) -> KeeperDecision:
    """把本轮被撤销的能力对应的字段清回默认值。

    没撤销任何能力时**原样返回同一个对象**（`sanitize_decision` 的行为），
    绝大多数轮次连一次拷贝都不发生。
    """
    if not revoked:
        return decision
    subject = Subject(
        id="keeper",
        kind="keeper",
        capabilities=ALL_CAPABILITIES - revoked,
        known_fact_ids=None,
    )
    return sanitize_decision(subject, decision)
