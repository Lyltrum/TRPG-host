"""Visibility 运行时骨架（路线第 5 步）。

静态配对表在 `ScenarioModule.visibility_pairs`（4b）。这里负责：
- 把「哪条配对已对谁揭开」记在 `keeper_state`（代码写，不靠 LLM 自由记账）；
- 每轮把密级状态注入局面块，供裁决/叙事遵守。

存储形态：逗号分隔的 `pair_id@observer` 条目。
- observer=`*`：房间级已揭开（当前叙事仍全员广播时，这是默认语义）
- observer=player_id：仅该观察者挣得（为真 per-player 预留；广播模型下
  叙事纪律仍靠 prompt，私密通道属后续协议）

🔴 叙事仍广播给全房间——真 per-observer 私信要 WS/协议层，本期不做。
"""

from __future__ import annotations

from app.core.keeper.module_loader import ScenarioModule

VISIBILITY_REVEALED_KEY = "已揭开配对"
ROOM_WIDE_OBSERVER = "*"


def load_revealed_visibility(keeper_state: dict | None) -> list[tuple[str, str]]:
    """解析 (pair_id, observer) 列表；保序、去空。"""
    if not keeper_state:
        return []
    raw = keeper_state.get(VISIBILITY_REVEALED_KEY)
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


def serialize_revealed_visibility(entries: list[tuple[str, str]]) -> str:
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


def format_visibility_status(
    module: ScenarioModule,
    revealed: list[tuple[str, str]],
    observer_id: str | None = None,
) -> str:
    """注入局面块的密级配对状态。无 pairs 时返回空串。"""
    if not module.visibility_pairs:
        return ""

    sealed: list[str] = []
    open_lines: list[str] = []
    for pair in module.visibility_pairs:
        note = f"（{pair.note}）" if pair.note else ""
        line = f"- {pair.id}：公开侧 {pair.public_ref} ↔ 真相侧 {pair.secret_ref}{note}"
        if is_pair_revealed(revealed, pair.id, observer_id):
            open_lines.append(line)
        else:
            sealed.append(line)

    parts: list[str] = []
    if sealed:
        parts.append(
            "### 尚未揭开（真相侧对玩家保密，不得在叙事中泄露 secret_ref 内容）\n"
            + "\n".join(sealed)
        )
    if open_lines:
        parts.append("### 已揭开（可将对应公开侧信息给玩家）\n" + "\n".join(open_lines))
    return "\n\n".join(parts)
