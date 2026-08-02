"""「各自所在」局面块。"""

from __future__ import annotations

from app.core.keeper.contract.registry import SituationContext
from app.core.keeper.runtime.location_state import format_party_locations


def render_party_locations(context: SituationContext) -> str:
    """全队同处一地、且没人在隐匿时返回空串——整块不渲染。

    单人局与未分头的多人局 prompt 与 P5.2 之前逐字一致（退化保证）。
    """
    return format_party_locations(context.module, context.keeper_state, list(context.players))
