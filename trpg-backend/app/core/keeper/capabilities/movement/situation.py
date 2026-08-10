"""「各自所在」局面块。"""

from __future__ import annotations

from app.core.keeper.contract.registry import SituationContext
from app.core.keeper.runtime.location_state import (
    format_party_locations,
    load_improvised_locations,
    resolve_location,
)


def render_party_locations(context: SituationContext) -> str:
    """全队同处一地、且没人在隐匿时返回空串——整块不渲染。

    单人局与未分头的多人局 prompt 与 P5.2 之前逐字一致（退化保证）。
    """
    return format_party_locations(context.module, context.keeper_state, list(context.players))


def render_improvised_locations(context: SituationContext) -> str:
    """这一局即兴建过的地点（exec/32）。一个都没有时整块不渲染（退化保证）。

    🔴 **必须全量列出，不许"只显示最近 N 条"**：这块就是模型挑 id 的白名单，
    没列出来的地点对它等于不存在——它会把「卡比家」重新申请一遍，一个地方两个
    id，同义词打地鼠当场复发。裁剪只能针对存储，不能针对展示（`exec/32 §7.2`）。
    """
    table = load_improvised_locations(context.keeper_state)
    if not table:
        return ""
    lines = []
    for loc_id, entry in table.items():
        origin = resolve_location(context.module, context.keeper_state, entry.get("from"))
        suffix = f"（从{origin}去的）" if origin else ""
        lines.append(f"- {loc_id}：{entry['name']}{suffix}")
    return "\n".join(lines)
