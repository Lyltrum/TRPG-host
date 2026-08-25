"""「现场」抽屉的组装（`exec/46` B4）。

把三样**已经有 id、但此前没有任何玩家出口**的东西凑成一个视图：
此刻在场的 NPC · 去过的地方 · 已揭开的线索。

🔴 **这一片真正的工作量是受众裁剪，不是取数**：

- **线索**自带 `audience`（事实账本从第一天就是逐人记的）⇒ 直接用
  `visible_fact_ids`，一行都不用新写。
- **在场 NPC 与去过的地方是房间级的**——`keeper_state` 里 `在场NPC` 是一串 id、
  `去过的节点` 是全队足迹，**没有任何字段记着"这个 NPC 站在哪一组面前"或
  "这个地方是谁去的"**。所以分头期间这两样一律不给（`split_now=True`），
  而不是照给。判据：**受众算错必须表现为"没人收到"，绝不退化成广播。**
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.keeper.capabilities.cast.state import load_on_stage
from app.core.keeper.memory.fact_ledger import visible_fact_ids
from app.core.keeper.runtime.location_state import group_players
from app.core.keeper.runtime.pending import MERGE_CONFIRM_KIND, pending_decision_manager
from app.dto.scene import SceneClueRead, SceneNpcRead, ScenePlaceRead, SceneRead
from app.models.room import Player, Room

#: 「去过的节点」的存储键。`closure` 那一片记的账，逗号分隔。
VISITED_KEY = "去过的节点"


def _load_visited(keeper_state: dict | None) -> list[str]:
    """解析足迹，保序去重。空串与缺键都是"还没去过任何地方"。"""
    if not keeper_state:
        return []
    raw = keeper_state.get(VISITED_KEY)
    if not raw:
        return []
    out: list[str] = []
    for part in str(raw).split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


async def get_scene(
    db: AsyncSession,
    room_id: str,
    player: Player,
    *,
    narrator: object | None = None,
) -> SceneRead:
    """组装这个玩家看得到的现场。

    `narrator`：只用来把 id 翻成名字/正文（剧本按房间加载，只有那一层知道该用
    哪一份）。拿不到时**原样给 id**，不编造名字——同 `location_label` 的先例。
    """
    room = await db.get(Room, room_id)
    keeper_state = (room.keeper_state if room is not None else None) or {}

    rows = await db.execute(select(Player.id).where(Player.room_id == room_id))
    all_ids = [pid for (pid,) in rows.all()]
    merge_pending = await pending_decision_manager.player_ids_of_kind(
        db, room_id, MERGE_CONFIRM_KIND
    )
    split_now = len(group_players(keeper_state, all_ids, merge_pending)) > 1

    # 🔴 分头时这两样直接不取（而不是取了再挡）：取到手再决定给不给，下一个人
    # 很容易在中间插一段"顺手也用一下"，那时泄漏就无声无息了。
    npc_ids = [] if split_now else load_on_stage(keeper_state)
    node_ids = [] if split_now else _load_visited(keeper_state)
    fact_ids = await visible_fact_ids(db, room_id=room_id, audience=frozenset({player.id}))

    labeler = getattr(narrator, "scene_labels", None)
    npc_names: dict[str, str] = {}
    place_names: dict[str, str] = {}
    clue_texts: dict[str, str] = {}
    if labeler is not None:
        npc_names, place_names, clue_texts = await labeler(
            room_id,
            keeper_state,
            npc_ids=npc_ids,
            node_ids=node_ids,
            fact_ids=fact_ids,
        )

    return SceneRead(
        npcs_on_stage=[SceneNpcRead(id=i, name=npc_names.get(i, i)) for i in npc_ids],
        visited_places=[ScenePlaceRead(id=i, name=place_names.get(i, i)) for i in node_ids],
        # 🔴 按账本的顺序给不了（`visible_fact_ids` 返回的是集合），所以按剧本
        # 里的文本排序会更稳；这里保持"有文本的才给"——解析不出模组时线索为空，
        # 那时给一串裸 fact-id 对玩家没有任何意义。
        clues=[SceneClueRead(id=i, text=t) for i, t in sorted(clue_texts.items())],
        split_now=split_now,
    )
