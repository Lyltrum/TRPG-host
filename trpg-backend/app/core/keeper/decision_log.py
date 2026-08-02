"""裁决留痕：把每一轮的分类与理由写进 `events`。

## 为什么要有

诊断 `exec/25 #59` 时拿不到那一轮 `player_state` 的实际值——`keeper.state` /
`keeper.node` / `narration.push` 都落了表，**唯独裁决本身没有**，而它才是
"叙事为什么这么写"的唯一解释。只能靠复现探针推断，而探针复现的是新的一次调用，
不是当时那次。

## 🔴 落什么、不落什么

`narration_guidance` 的**内容一个字都不落**，只落哪几条代码强制命中了
（`forced`）。guidance 里有"须保密什么"，而 `get_replay` 是把 `payload` 原样
返回给玩家的。虽然 replay 已经显式排除了本事件类型，但不把敏感内容写进去是
更靠前的一道——纵深防御，同「保密靠拿不到，不是请你别说」。

`thinking` 同理是审计字段（prompt 里明写玩家看不到），它落表**只**因为 replay
那条排除；两道都在。

各能力自带的留痕字段由 `audit` 钩子汇总进来，不必逐片在这里列（`exec/27`）。
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.keeper.capabilities import audit_fields
from app.core.keeper.decision import KeeperDecision
from app.models.event import Event

logger = structlog.get_logger()


async def record_decision(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    room_id: str,
    player_id: str,
    decision: KeeperDecision,
    forced: list[str],
) -> None:
    """把这一轮的裁决分类与理由写进 events（exec/25 #61）。

    为什么要有：诊断 exec/25 #59 时拿不到那一轮 `player_state` 的实际值——
    `keeper.state`/`keeper.node`/`narration.push` 都落了表，唯独**裁决本身
    没有**，而它才是"叙事为什么这么写"的唯一解释。只能靠复现探针推断，而
    探针复现的是新的一次调用，不是当时那次。

    🔴 **不落 `narration_guidance` 的内容，只落哪几条代码强制命中了。**
    guidance 里有"须保密什么"，而 `get_replay` 是把 `payload` 原样返回给
    玩家的。虽然 replay 已经显式排除了本事件类型，但不把敏感内容写进去是
    更靠前的一道——纵深防御，同「保密靠拿不到，不是请你别说」。
    `thinking` 同理是审计字段（prompt 里明写玩家看不到），它落表**只**因为
    replay 那条排除；两道都在。

    任何失败都不能连累这一轮：留痕挂了，游戏照常进行。
    """
    try:
        async with session_factory() as db:
            db.add(
                Event(
                    room_id=room_id,
                    player_id=player_id,
                    event_type="keeper.decision",
                    payload={
                        "player_state": decision.player_state,
                        "thinking": decision.thinking,
                        "forced": forced,
                        "current_node_id": decision.current_node_id,
                        # 已切出去的能力自带留痕字段（exec/27 阶段 3 · A 族）。
                        # 跟 keeper_decision 日志复用同一份——否则每加一片
                        # 能力，它的裁决在 events 表里就没有痕迹，且不报错。
                        **audit_fields(decision),
                    },
                )
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — 留痕失败不该让真人的这一轮失败
        logger.warning("keeper_decision_record_failed", room_id=room_id, error=str(exc))
