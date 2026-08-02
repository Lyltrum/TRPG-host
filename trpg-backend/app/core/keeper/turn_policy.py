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

from dataclasses import dataclass

from app.core.keeper.decision import KeeperDecision
from app.core.keeper.prose_discipline import (
    inject_action_resolution_guidance,
    inject_confusion_guidance,
    inject_feasibility_question_guidance,
    inject_kp_question_guidance,
    inject_spotlight_guidance,
    inject_weird_response_guidance,
    is_clear_action_intent,
    is_player_confused,
    is_violence_edge_utterance,
    is_weird_or_meta_utterance,
)
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


@dataclass(frozen=True)
class TurnClassification:
    """本轮玩家发言属于哪一类——**互斥**，顺序即优先级。

    分类信号来自裁决 LLM 在同一次调用里顺手给出的 `player_state` 字段。只有
    裁决完全失败（走兜底文案、此时 `player_state` 只是默认值 "normal"、不可信）
    才退回正则作为安全网。

    🔴 2026-07-29 换掉正则的理由：正则要求关键词字面严格相邻（"我该"必须紧邻），
    真人实测"我现在该做什么"（插了"现在"）就匹配不上。**那是正则做语义分类的
    结构性上限，不是"这条正则不够全"。**
    """

    #: 裁决整个失败了（分类退回正则）。
    adjudicate_fallback: bool = False
    #: 玩家在问"我该做什么"这类元问题。
    confused: bool = False
    #: 开玩笑 / OOC / 越狱套话。
    weird: bool = False
    #: 明确宣告了具体动作。
    clear_action: bool = False
    #: 向守秘人打听他角色本该知道的设定（要的是**回忆**）。
    kp_question: bool = False
    #: 征询可行性或许可（要的是**一个答案**，还没决定做）。
    feasibility_question: bool = False
    #: 对他人动手或强行突破（成败必须由骰子决定）。
    physical_conflict: bool = False

    @property
    def asks_kp(self) -> bool:
        """两类提问共用"收走推进世界的手段"，但 guidance 分开。"""
        return self.kp_question or self.feasibility_question

    def forced_labels(self, *, spotlight: bool) -> list[str]:
        """落进 `keeper.decision` 事件的 `forced`：哪几条代码强制命中了。

        顺序固定（不是集合），复盘时一眼能对上分支优先级。
        """
        return [
            name
            for name, hit in (
                ("kp_question", self.kp_question),
                ("feasibility_question", self.feasibility_question),
                ("confused", self.confused),
                ("weird_or_meta", self.weird),
                ("clear_action", self.clear_action),
                ("physical_conflict", self.physical_conflict),
                ("adjudicate_fallback", self.adjudicate_fallback),
                ("spotlight", spotlight),
            )
            if hit
        ]


def classify_turn(
    decision: KeeperDecision,
    utterance: str,
    *,
    fallback_guidance: str,
    is_heartbeat: bool,
    is_opening_ceremony: bool,
) -> TurnClassification:
    """把裁决输出翻译成这一轮的分类。纯函数，不碰 IO。

    `physical_conflict` 与 `asks_kp` 都排除心跳/开场——那两种轮次没有"玩家这一
    句话"可分类。`confused`/`weird`/`clear_action` **不**在这里排除，它们的模式
    限制写在 `apply_code_forcing` 的分支条件上（与拆分前逐字一致）。
    """
    adjudicate_fallback = decision.narration_guidance == fallback_guidance
    if adjudicate_fallback:
        confused = is_player_confused(utterance)
        weird = is_weird_or_meta_utterance(utterance)
        clear_action = is_clear_action_intent(utterance)
    else:
        confused = decision.player_state == "confused"
        weird = decision.player_state == "weird_or_meta"
        clear_action = decision.player_state == "clear_action"

    live = not adjudicate_fallback and not is_heartbeat and not is_opening_ceremony
    return TurnClassification(
        adjudicate_fallback=adjudicate_fallback,
        confused=confused,
        weird=weird,
        clear_action=clear_action,
        kp_question=live and decision.player_state == "question_to_kp",
        feasibility_question=live and decision.player_state == "feasibility_question",
        physical_conflict=live and decision.player_state == "physical_conflict",
    )


def apply_code_forcing(
    decision: KeeperDecision,
    classification: TurnClassification,
    *,
    utterance: str,
    spotlight_nickname: str | None,
    is_heartbeat: bool,
    is_opening_ceremony: bool,
    revoked: frozenset[Capability] = frozenset(),
) -> KeeperDecision:
    """代码强制：注入 guidance + 撤销本轮不该有的能力。纯函数，不碰 IO。

    🔴 **这条 if/elif 的顺序就是语义**，拆错会**静默**改变行为——某一类发言从此
    走错分支，没有任何东西会报错。`tests/test_turn_classification_characterization.py`
    把整个矩阵逐格钉死了，改这里之前先读它。

    优先级：提问 > 迷茫 > 怪话 > 明确行动。聚光灯与它们**叠加**，不参与三选一
    ——被冷落跟他说的那句话是什么类型无关。
    """
    revoked_now: set[Capability] = set(revoked)

    if classification.asks_kp:
        # 收走推进世界的手段：两类共用。guidance 分开——见 prose_discipline
        # 里那两段 prefix 的注释。
        inject = (
            inject_feasibility_question_guidance
            if classification.feasibility_question
            else inject_kp_question_guidance
        )
        revoked_now |= CHECK_CAPABILITIES | SCENE_ADVANCE_CAPABILITIES
        decision = decision.model_copy(
            update={"narration_guidance": inject(decision.narration_guidance)}
        )
    elif classification.confused:
        # 🔴 裁决走兜底时不要把它和迷茫引导拼一起——兜底文案说"别编造+可请玩家
        # 重说一遍"，迷茫引导说"必须给 1-2 个具体方向"，两句话方向相反，叙事
        # 模型会各退一步、缩回复述已知信息这个最安全选项（真人实测 2026-07-28
        # 复现：玩家问"该做什么"，回复是前情复述而非建议）。迷茫引导本身已自洽
        # （给方向不需要先问清楚），兜底走这条分支时直接丢弃、不拼接。
        base = "" if classification.adjudicate_fallback else decision.narration_guidance
        revoked_now |= CHECK_CAPABILITIES
        decision = decision.model_copy(
            update={"narration_guidance": inject_confusion_guidance(base)}
        )
    elif classification.weird and not is_heartbeat and not is_opening_ceremony:
        # 怪话接招：元/玩笑清检定；暴力边界保留检定（伤害/SAN）但同样强制接招
        if not is_violence_edge_utterance(utterance):
            revoked_now |= CHECK_CAPABILITIES
        decision = decision.model_copy(
            update={
                "narration_guidance": inject_weird_response_guidance(decision.narration_guidance)
            }
        )
    elif classification.clear_action and not is_heartbeat and not is_opening_ceremony:
        # 明确行动：强制推进，禁止街景挡枪（全模组通用）
        decision = decision.model_copy(
            update={
                "narration_guidance": inject_action_resolution_guidance(decision.narration_guidance)
            }
        )

    decision = revoke(decision, frozenset(revoked_now))

    # 聚光灯（exec/14 P5.2）：导演层算出"谁最久没被点到"，这里强制注入。
    if spotlight_nickname:
        decision = decision.model_copy(
            update={
                "narration_guidance": inject_spotlight_guidance(
                    decision.narration_guidance, spotlight_nickname
                )
            }
        )
    return decision
