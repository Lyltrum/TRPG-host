"""线索揭示（clue_reveal）的运行时状态（路线第 5 步）。

静态配对表在 `ScenarioModule.visibility_pairs`（4b）。这里负责：
- 把「哪条配对已对谁揭开」记在 `keeper_state`（代码写，不靠 LLM 自由记账）；
- 每轮把密级状态注入局面块，供裁决/叙事遵守。

存储形态：逗号分隔的 `pair_id@observer` 条目。
- observer=`*`：房间级已揭开（当前叙事仍全员广播时，这是默认语义）
- observer=player_id：仅该观察者挣得（为真 per-player 预留；广播模型下
  叙事纪律仍靠 prompt，私密通道属后续协议）

🔴 叙事仍广播给全房间——真 per-observer 私信要 WS/协议层，本期不做。

## 🔴 改名的边界（exec/27 三处撞名）

`stealth`（技能）/`stealth`（状态）/`visibility`（线索）三个名字互相干扰。这里
把**代码里的**名字改成 `clue_reveal` / `clues_revealed`，但两样东西**不动**：

- `ScenarioModule.visibility_pairs`：模组数据，改了要迁移五个模组的
  structured.json，而"配对表"这个词本身不歧义；
- `CLUES_REVEALED_KEY` 的**字符串值**仍是 `"已揭开配对"`：那是 `keeper_state`
  里的键，改了会让已经在跑的房间读不到自己的记录。改的只是 Python 标识符。
"""

from __future__ import annotations

from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.contract.registry import SituationContext
from app.core.keeper.runtime.progress_state import (
    CLUES_REVEALED_KEY,
    ROOM_WIDE_OBSERVER,
    is_pair_revealed,
    load_revealed_clues,
    serialize_revealed_clues,
)

# 🔴 键与解析已下沉到 `runtime/progress_state.py`（`closure` 也要读它们，而
# 能力之间不许互相 import）。这里重新导出，让既有调用点与写入侧不必改写法；
# 留在本文件的是**这片能力怎么把状态说给模型听**，那是它自己的表达。
__all__ = [
    "CLUES_REVEALED_KEY",
    "ROOM_WIDE_OBSERVER",
    "format_clue_status",
    "is_pair_revealed",
    "load_revealed_clues",
    "render_clue_status",
    "serialize_revealed_clues",
]


def format_clue_status(
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


def render_clue_status(context: SituationContext) -> str:
    """注册进局面块的 situation 钩子。

    🔴 **本能力是 `SituationContext` 的成因**：它要按观察者渲染（哪条配对对
    **这个玩家**揭开了），而钩子第一版签名只有「剧本 + keeper_state」两个参数。
    """
    return format_clue_status(
        context.module,
        load_revealed_clues(context.keeper_state),
        observer_id=context.observer_id,
    )
