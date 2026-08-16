"""「他在这个地方已经掷过几次这个技能」的记账与注入（2026-08-16 真机）。

## 🔴 为什么需要它

实测一局 17 次检定里，**侦察占了 9 次**，而且反复砸在同一个目标上：
「看清地下室里有什么」要了 4 次，钻进墙洞之后又连着 5 次。每失败一次守秘人
就加一层新遮挡——灰 → 水汽 → 眼睛要适应 → 雾更重了 → 拐角 → 更窄——玩家最后
只能说「我不看了，直接爬进去」才推得动。

规则 1d 写的是「失败改代价，别没收线索」，而模型把"代价"实现成了**再加一道
帘子**；**没有任何规则说「同一件事失败之后不许再要同一个检定」**。

## 🔴 根因跟 SAN 那条一模一样：规则可以有，但数据从来没进过上下文

跟 `san_check` 那次同族——"已经掷过了"这件事模型无从知道。区别在于**门的
形状不一样**：SAN 的失控发生在**同一拍之内**（结算叙事又开一次裁决），所以
那里能用「一拍之内只掷一次」拦掉；而这里 9 次侦察**各自跟在一句新的玩家发言
后面**，量出来分属 9 个不同的拍，那道门的形状在这里根本不适用。

能用的抓手是另一个：**（节点 id, 技能 id）都是 id**。实测 9 次里 7 次落在
两个节点上（4 + 3）。所以这里做的是**注入**不是拦截——按「能确定化的是判断
的输入，不是判断本身」：把次数摆到它眼前，判不判仍归它。

不拦的第二个理由：COC7 里情况实质改变后重掷是合法的，孤注一掷更是明写的
机制。拦下去会误伤，而误伤的代价（玩家被卡住）比多掷一次更贵。
"""

from __future__ import annotations

from app.core.keeper.contract.registry import SituationContext

#: `keeper_state` 里的记账键。值形如 `{"节点id|技能id": {"n": 3, "fail": 2}}`。
#: 跟 `即兴地点` 一样是嵌套结构——保留键不受"值必须是 str"那条约束。
CHECK_ATTEMPTS_KEY = "本地检定次数"

#: 少于这个次数不进局面块：掷过一次是常态，说出来只是噪音。
_MIN_ATTEMPTS_TO_MENTION = 2


def _key(node_id: str, skill_id: str) -> str:
    return f"{node_id}|{skill_id}"


def load_attempts(keeper_state: dict | None) -> dict[str, dict[str, int]]:
    if not keeper_state:
        return {}
    raw = keeper_state.get(CHECK_ATTEMPTS_KEY)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        out[str(k)] = {"n": int(v.get("n") or 0), "fail": int(v.get("fail") or 0)}
    return out


def record_attempt(
    table: dict[str, dict[str, int]], node_id: str, skill_id: str, *, succeeded: bool
) -> dict[str, dict[str, int]]:
    """记一次尝试。返回**新表**，不原地改——调用方要整列写回 `keeper_state`。"""
    out = {k: dict(v) for k, v in table.items()}
    entry = out.setdefault(_key(node_id, skill_id), {"n": 0, "fail": 0})
    entry["n"] += 1
    if not succeeded:
        entry["fail"] += 1
    return out


def format_attempts(context: SituationContext) -> str:
    """局面块：这一处已经掷过哪些技能、掷了几次、失败几次。

    只列**当前所在节点**的那几条——别处掷过多少跟这一拍的判断无关，全列出来
    就是拿噪音换信息。
    """
    from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY

    state = context.keeper_state or {}
    here = state.get(CURRENT_NODE_KEY)
    if not here:
        return ""
    lines: list[str] = []
    for key, entry in load_attempts(state).items():
        node_id, _, skill_id = key.partition("|")
        if node_id != here or entry["n"] < _MIN_ATTEMPTS_TO_MENTION:
            continue
        lines.append(f"- {skill_id}：已经掷过 {entry['n']} 次，其中失败 {entry['fail']} 次")
    if not lines:
        return "\n".join(lines)
    return (
        "【他在这一处已经反复掷过的检定】\n"
        + "\n".join(lines)
        + "\n同一件事不要一再要检定：**已经失败过的，除非情境实质改变（换了工具、"
        "有人帮忙、门被打开了、目标自己动了），否则不要再要同一个检定**——"
        "直接按已有结果往下写，或者给一条别的路。"
    )
