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

#: 存储键。代码记账，不原样喂给模型（走 `reserved_state_keys`）。
ESTABLISHED_KEY = "既成事实"

#: 已经发到第几号。
ESTABLISHED_SEQ_KEY = "既成事实序号"

FACT_ID_PREFIX = "fact-"

#: 超过这个条数打一条 warning。**膨胀本身是信号，不是要治的病**——判据同即兴
#: 地点与悬而未决：它说明模型在拿这张表当便签本，那时该查的是"它把什么塞进
#: 来了"，而不是给这张表加裁剪。真要裁，也只能裁存储不能裁展示。
ESTABLISHED_SOFT_LIMIT = 24


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
    return "\n".join(f"- {entry['text']}" for _fact_id, entry in sorted(table.items()))
