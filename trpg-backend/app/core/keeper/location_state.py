"""per-player 位置（exec/14 P5.2 的共用地基）。

## 为什么位置要按人存

私密性的根源只有一个：**你不在场，所以你不知道**（exec/18 定稿）。①分头
探索、②潜行躲藏、⑥玩家主动私密三件事共用同一套 per-observer 投递，而投递
的分组依据就是位置——位置不按人存，这三件事一件都做不了。

## 与房间级「当前场景节点」的关系

`scene_state.CURRENT_NODE_KEY` 保留原样，语义收窄为**大部队所在**：
- 本表（`PLAYER_LOCATION_KEY`）记的是**逐人**位置，只在有人被显式定位过时才有条目；
- 查一个人的位置：先查本表，查不到**回落到房间级指针**。

这个回落是**有定义的默认值**，不是静默兜底：「没有被单独定位过的调查员，
就跟大部队在一起」是这个模型里唯一说得通的语义，而且它让 P5.2 之前建的
房间（只有房间级指针）行为逐字不变。**查不到位置返回 None**——上层不得
把 None 当成"跟谁都在一起"，全 None 时整桌就是一组（见 group_players）。

## 存储形态

跟 `visibility.py` 同一套：keeper_state 里一个逗号分隔的字符串，
`player_id@node_id`。这个键在 `tools._RESERVED_STATE_KEYS` 里，LLM 的
`state_updates` 改不动它。
"""

from __future__ import annotations

from app.core.keeper.module_loader import ScenarioModule
from app.core.keeper.scene_state import load_current_node_id

PLAYER_LOCATION_KEY = "玩家位置"

#: 潜行/躲藏中的调查员（exec/18 ②）。「在场但不可见」——他照常**听得见**这里
#: 发生的一切（所以不把他从位置分组里摘出去），但他自己的行动不会广播给同处
#: 的其他人。存 player_id 的逗号串。
HIDDEN_PLAYERS_KEY = "隐匿玩家"


def load_player_locations(keeper_state: dict | None) -> dict[str, str]:
    """解析 player_id → node_id。保序、去空、后写覆盖先写。"""
    if not keeper_state:
        return {}
    raw = keeper_state.get(PLAYER_LOCATION_KEY)
    if raw is None or raw == "":
        return {}
    out: dict[str, str] = {}
    for part in str(raw).split(","):
        part = part.strip()
        if not part or "@" not in part:
            continue
        player_id, node_id = part.split("@", 1)
        player_id, node_id = player_id.strip(), node_id.strip()
        if player_id and node_id:
            out[player_id] = node_id
    return out


def serialize_player_locations(locations: dict[str, str]) -> str:
    return ", ".join(f"{pid}@{nid}" for pid, nid in locations.items())


def load_hidden_players(keeper_state: dict | None) -> set[str]:
    if not keeper_state:
        return set()
    raw = keeper_state.get(HIDDEN_PLAYERS_KEY)
    if raw is None or raw == "":
        return set()
    return {part.strip() for part in str(raw).split(",") if part.strip()}


def serialize_hidden_players(player_ids: set[str]) -> str:
    return ", ".join(sorted(player_ids))


def location_of(keeper_state: dict | None, player_id: str) -> str | None:
    """这名调查员现在在哪个剧本节点。逐人表优先，回落房间级指针（见模块 docstring）。"""
    located = load_player_locations(keeper_state).get(player_id)
    if located:
        return located
    return load_current_node_id(keeper_state)


def group_players(
    keeper_state: dict | None, player_ids: list[str]
) -> list[tuple[str | None, list[str]]]:
    """把一组玩家按位置分组，返回 [(node_id, [player_id, ...]), ...]。

    保 `player_ids` 的入参顺序（分组顺序 = 各组第一个人出现的顺序），叙事
    分段的顺序因此是确定的、可断言的。位置未知（None）自成一组——**不要**
    把它们并进任何已知位置的组，"不知道他在哪"不等于"他在这儿"。
    """
    groups: dict[str | None, list[str]] = {}
    for pid in player_ids:
        groups.setdefault(location_of(keeper_state, pid), []).append(pid)
    return list(groups.items())


def is_party_split(keeper_state: dict | None, player_ids: list[str]) -> bool:
    """全队是否已分头。单人局恒为 False（退化保证：一个人分不了头）。"""
    return len(group_players(keeper_state, player_ids)) > 1


def format_party_locations(
    module: ScenarioModule,
    keeper_state: dict | None,
    players: list[tuple[str, str]],
) -> str:
    """注入局面块的「各自所在」。

    **全队同处一地、且没人在隐匿时返回空串**——整块不渲染，单人局与未分头的
    多人局 prompt 与 P5.2 之前逐字一致（退化保证）。只有真的分头/有人藏起来了，
    裁决器才需要知道谁在哪、谁看不见谁。
    """
    ids = [pid for pid, _ in players]
    groups = group_players(keeper_state, ids)
    hidden = load_hidden_players(keeper_state)
    if len(groups) <= 1 and not hidden.intersection(ids):
        return ""
    nicknames = dict(players)
    lines: list[str] = []
    for node_id, members in groups:
        node = module.node_by_id(node_id) if node_id else None
        where = f"{node.title}（{node_id}）" if node is not None else (node_id or "（位置未记录）")
        who = "、".join(
            f"{nicknames.get(pid, pid)}（隐匿中）" if pid in hidden else nicknames.get(pid, pid)
            for pid in members
        )
        lines.append(f"- {where}：{who}")
    return "\n".join(lines)
