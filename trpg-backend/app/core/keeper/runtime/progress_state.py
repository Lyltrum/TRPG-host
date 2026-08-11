"""「这一局走到哪了」的三份记账：已揭开配对 / 已触发议程 / 去过的节点。

## 为什么在 runtime 而不是各自的能力目录里

判据是那句老的：**共享的状态与它的读写归 runtime，用它做裁决的字段与执行
归能力**（同 `phase.py` / `location_state.py`）。

前两份原本各自躺在 `clue_reveal/pairs.py` 与 `agenda/state.py` 里，那时它们
确实只有一个消费者。`closure` 能力出现之后不再是——它要数「还有多少没揭开、
还有多少没触发」，而**能力之间不许互相 import**。所以键与解析下沉到这里，
各能力保留自己的 `format_* / render_*`（那是这片能力怎么把状态说给模型听，
属于它自己的表达，不是共享状态）。

写入侧仍在各自能力的 `executor.py`，那是唯一允许改这些键的地方；键本身通过
`reserved_state_keys` 钩子声明出去，`state_updates` 碰不到。

🔴 **键的字符串值不许改**：它们是已经在跑的房间的 `keeper_state` 里的键，
改一个字就等于让那些房间读不到自己的记录。改 Python 标识符随意。
"""

from __future__ import annotations

CLUES_REVEALED_KEY = "已揭开配对"
ROOM_WIDE_OBSERVER = "*"

AGENDA_FIRED_KEY = "已触发议程"

VISITED_NODES_KEY = "去过的节点"


# ── 已揭开配对 ──────────────────────────────────────


def load_revealed_clues(keeper_state: dict | None) -> list[tuple[str, str]]:
    """解析 (pair_id, observer) 列表；保序、去空。"""
    if not keeper_state:
        return []
    raw = keeper_state.get(CLUES_REVEALED_KEY)
    if raw is None or raw == "":
        return []
    out: list[tuple[str, str]] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        if "@" in part:
            pair_id, observer = part.split("@", 1)
            pair_id, observer = pair_id.strip(), observer.strip() or ROOM_WIDE_OBSERVER
        else:
            pair_id, observer = part, ROOM_WIDE_OBSERVER
        if pair_id:
            out.append((pair_id, observer))
    return out


def serialize_revealed_clues(entries: list[tuple[str, str]]) -> str:
    return ", ".join(f"{pid}@{obs}" for pid, obs in entries)


def is_pair_revealed(
    entries: list[tuple[str, str]],
    pair_id: str,
    observer_id: str | None = None,
) -> bool:
    """房间级揭开，或对指定 observer 揭开，都算已揭开。"""
    for pid, obs in entries:
        if pid != pair_id:
            continue
        if obs == ROOM_WIDE_OBSERVER:
            return True
        if observer_id is not None and obs == observer_id:
            return True
    return False


# ── 已触发议程 ──────────────────────────────────────


def load_fired_agenda(keeper_state: dict | None) -> list[str]:
    """从状态笔记里解析已触发的议程 id（纯函数，无 IO）。

    存储形态是逗号分隔字符串——keeper_state 的值一律是 str（`update_state_impl`
    的契约），不为一个列表破例。None / 缺 key / 空串 / 尾逗号都要稳健解析。
    """
    if not keeper_state:
        return []
    raw = keeper_state.get(AGENDA_FIRED_KEY)
    if raw is None or raw == "":
        return []
    # 去空白、去空项、保序（一旦写入顺序就是触发顺序，审计用得上）。
    return [part.strip() for part in str(raw).split(",") if part.strip()]


# ── 去过的节点 ──────────────────────────────────────


def load_visited_nodes(keeper_state: dict | None) -> list[str]:
    """走过哪些剧本节点，保序去重。

    此前**没有任何地方记这个**——`keeper_state` 只存「当前」在哪，人一走过去
    就没了痕迹。而「还有多少地方没去过」正是判断"内容跑完没有"的三个数之一。
    """
    if not keeper_state:
        return []
    raw = keeper_state.get(VISITED_NODES_KEY)
    if raw is None or raw == "":
        return []
    out: list[str] = []
    for part in str(raw).split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def serialize_visited_nodes(node_ids: list[str]) -> str:
    return ", ".join(node_ids)
