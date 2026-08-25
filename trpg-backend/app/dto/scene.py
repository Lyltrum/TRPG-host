"""「现场」抽屉的读模型（`exec/46` B4）。

回答的是「我此刻站在哪、旁边有谁、走过哪些地方、挣到过什么」——**全部都是
系统已经有 id 的东西**，此前一样都没有通往玩家的出口。

🔴 **位置那一半不在这里**：`party.update` 每轮推的「当前所在 · 同处的人 ·
另有几组」已经解决了它，而且后端逐人裁过。这个端点只补它没有的三样，
**不重复给一份**（同判据「一份数据有两个出口就有两份会分叉的真相」）。
"""

from app.dto.common import CamelModel


class SceneNpcRead(CamelModel):
    """此刻在台上的一个 NPC。"""

    id: str
    #: 解析不出模组时**原样给 id，不编造名字**（同 `location_label` 的先例）。
    name: str


class ScenePlaceRead(CamelModel):
    """去过的一个地方。"""

    id: str
    name: str


class SceneClueRead(CamelModel):
    """一条已经揭开、并且**这个玩家知道**的线索。"""

    id: str
    text: str


class SceneRead(CamelModel):
    """GET /api/v1/rooms/{roomId}/scene 返回。

    🔴 **四个字段一个默认值都不给**：服务端每次都送得出，契约就该说它一定在。
    给了默认值 = 生成的 TS 是可选的 = 前端只能 `?? []`（2026-08-19 一天踩两次）。
    """

    npcs_on_stage: list[SceneNpcRead]
    visited_places: list[ScenePlaceRead]
    clues: list[SceneClueRead]
    #: 这一刻队伍是**分头**的（不止一组人）。
    #:
    #: 🔴 分头时 `npcs_on_stage` 与 `visited_places` 一律为空，**这是设计不是
    #: 缺陷**：这两样在 `keeper_state` 里是**房间级**的（`在场NPC` 是一串 id、
    #: `去过的节点` 是全队足迹），没有任何字段记着"这个 NPC 站在哪一组面前"
    #: 或"这个地方是谁去的"。分头期间照给就是把别处那一组的处境泄给你——
    #: 而**受众算错必须表现为"没人收到"，绝不退化成广播**。
    #:
    #: 前端据此说明为什么这两段是空的，而不是显示成"这里没有人"。
    #: 线索不受影响：事实账本自带 `audience`，本来就是逐人裁的。
    split_now: bool
