"""既成事实的存储形态 + 局面块。

## 跟 `open_threads` 的分工 = 生命周期正好相反

| | `open_threads` 悬而未决 | 本片 既成事实 |
|---|---|---|
| 语义 | 还没了结，**需要你继续演** | 已经了结，**后果永远为真** |
| 结清动作 | 有 `resolved_threads` | **没有** |
| 会不会消失 | 会（`pop` 掉） | 不会 |

🔴 **没有结清动作是这一片存在的全部理由。** 把「烧掉的木屋」塞进 threads 的话，
模型迟早会把它标成已解决——`resolve_threads_impl` 是 `pop`，那条记忆当场蒸发。
两者生命周期相反，合并就等于假装它们一样。

## 为什么不是 `state_updates`

`exec/40` ④ 把世界状态的键收进了白名单，即兴的东西写不进去了——而那次收口
**反过来让这一片变得必要**：在那之前模型还能把「屋子烧了」塞进一个自由键
（记得住，但永远不会被清理、而且代码不认识）；收口之后它没有任何落点。

## 形态照抄，不新造

模型给文本、代码分配 id（`fact-N`）、局面块全量列出。这是这套形态的**第三个
实例**（即兴地点 `exec/32` → 悬而未决 `exec/36` → 本片），所以是照抄。

🔴 **序号只增不复用**，理由同 `open_threads`：虽然这张表的条目不会被删，
但"从表里现算最大号"这个写法本身是错的形状，照抄正确的那个。
"""

from __future__ import annotations

import difflib

#: 存储键。代码记账，不原样喂给模型（走 `reserved_state_keys`）。
ESTABLISHED_KEY = "既成事实"

#: 已经发到第几号。
ESTABLISHED_SEQ_KEY = "既成事实序号"

FACT_ID_PREFIX = "fact-"

#: 超过这个条数打一条 warning。**膨胀本身是信号，不是要治的病**——判据同即兴
#: 地点与悬而未决：它说明模型在拿这张表当便签本，那时该查的是"它把什么塞进
#: 来了"，而不是给这张表加裁剪。真要裁，也只能裁存储不能裁展示。
ESTABLISHED_SOFT_LIMIT = 24


#: 两条既成事实文本相似到这个程度就当**同一件事**，新的那条直接丢掉。
#:
#: 🔴 **阈值是拿真样本标定的，而标定结果推翻了「一个阈值就够」**（2026-08-18
#: 双人真机）：
#:
#:   - 真重复 A（这一局）`程雨眠用撬棍砸碎了…渡轮模型` vs `程雨眠砸碎了…渡轮
#:     模型` ⇒ **0.923**
#:   - 真重复 B（08-16）`点燃了地下室的煤油` vs `点燃了地下室，火势已起` ⇒ **0.600**
#:   - 而**不该判重复**的「同句式、换了主语与宾语」（`程雨眠砸碎了…渡轮模型`
#:     vs `霍启元砸碎了…神像`）⇒ **0.647**，**夹在两个真样本中间**。
#:
#: ⇒ 一个阈值抓不全。既成事实是**永久记忆**，误删一条真事实的代价高于漏拦一条
#: 重复（漏拦只是局面块多一行），所以按仓库既有的两档做（同 `_entity_name_in_key`
#: 的先例）：**确定的那一半拦，判不准的报而不断**。
#:
#: ⚠️ 上面那条负样本是**构造的，不是观测到的**——真出现了误伤要先看它长什么样，
#: 别直接调数。
DUPLICATE_RATIO = 0.85

#: 到这个程度就**只报不拦**：像但不够像，记下来供事后看它到底该不该拦。
SUSPECT_RATIO = 0.60


def duplicate_of(table: dict[str, dict], text: str) -> tuple[str | None, str | None]:
    """这条新事实跟表里哪一条重了。返回 (要拦的 fact_id, 只报不拦的 fact_id)。

    两个返回值最多有一个非空；都为空表示这是一条新事实。
    """
    best_id, best_ratio = None, 0.0
    for fact_id, entry in table.items():
        ratio = difflib.SequenceMatcher(None, entry["text"], text).ratio()
        if ratio > best_ratio:
            best_id, best_ratio = fact_id, ratio
    if best_id is None:
        return None, None
    if best_ratio >= DUPLICATE_RATIO:
        return best_id, None
    if best_ratio >= SUSPECT_RATIO:
        return None, best_id
    return None, None


def load_established(keeper_state: dict | None) -> dict[str, dict]:
    """解析既成事实表。形状不对的条目整条丢弃，不产生半条记录。"""
    if not keeper_state:
        return {}
    raw = keeper_state.get(ESTABLISHED_KEY)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for fact_id, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        text = str(payload.get("text") or "").strip()
        if not fact_id or not text:
            continue
        out[str(fact_id)] = {"text": text}
    return out


def load_fact_seq(keeper_state: dict | None) -> int:
    raw = (keeper_state or {}).get(ESTABLISHED_SEQ_KEY)
    if isinstance(raw, int) and raw >= 0:
        return raw
    return _max_id_in(load_established(keeper_state))


def _max_id_in(table: dict[str, dict]) -> int:
    used = 0
    for fact_id in table:
        if fact_id.startswith(FACT_ID_PREFIX):
            suffix = fact_id[len(FACT_ID_PREFIX) :]
            if suffix.isdigit():
                used = max(used, int(suffix))
    return used


def next_fact_id(seq: int) -> tuple[str, int]:
    """下一个 id 与新的计数。只增不复用。"""
    return f"{FACT_ID_PREFIX}{seq + 1}", seq + 1


def format_established(context) -> str:  # noqa: ANN001 — SituationContext，避免导入环
    """局面块正文。一条都没有时返回空串——整块不渲染（退化保证）。

    🔴 **全量列出**，理由同悬而未决：没列出来的对模型等于不存在。而这一片
    比那一片更需要全量——它的全部价值就是"十几小时之后还记得"。
    """
    table = load_established(context.keeper_state)
    if not table:
        return ""
    # 🔴 **带 id 列出来**（2026-08-16 真机调整）：第一版只渲染文本，实测同一件事
    # 被记了两遍（`fact-1 点燃了地下室的煤油` / `fact-2 点燃了地下室，火势已起`，
    # 分别来自相邻的两拍）。悬而未决那一片天然没这个毛病，因为它必须显示 `thread-N`
    # 供 `resolved_threads` 引用——**id 顺带承担了"这条已经有了"的信号**。
    # 这一片没有结清动作，所以 id 的唯一作用就是这个，但它确实是必要的。
    #
    # 🔴 **但 id 不够**（2026-08-18 双人真机推翻了上面那句的言下之意）：同一拍的
    # 两次裁决各记了一条"程雨眠砸碎了渡轮模型"，而第二次裁决**看得见** `fact-2`
    # （`_load_room_memory` 每次开新 session，不是脏读）。⇒ 把已有的摆出来只是
    # 必要条件，去重得由代码做，见 `duplicate_of`。
    rows = sorted(table.items(), key=lambda kv: _order_of(kv[0]))
    return "\n".join(f"- {fact_id}｜{entry['text']}" for fact_id, entry in rows)


def _order_of(fact_id: str) -> tuple[int, str]:
    """按 `fact-N` 的数字排，不是按字符串——不然 fact-10 会排在 fact-2 前面。"""
    if fact_id.startswith(FACT_ID_PREFIX):
        suffix = fact_id[len(FACT_ID_PREFIX) :]
        if suffix.isdigit():
            return (int(suffix), "")
    return (10**9, fact_id)
