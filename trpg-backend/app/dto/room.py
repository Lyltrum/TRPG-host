"""Room 模块的 pydantic 请求/响应模型。

对应的 TS 类型由 trpg-sdk 的 codegen 脚本从这些模型生成（见 scripts/export_schema.py
和 issue #75），不再需要手动同步 trpg-sdk/src/types.ts。

命名约定：
- 后端代码内统一使用 snake_case Python 命名
- 通过 alias_generator 实现 JSON 层的 camelCase ↔ snake_case 自动映射
- 请求（camelCase JSON → snake_case Python）和响应（snake_case Python → camelCase JSON）
  由 pydantic 自动处理，业务代码无需关心
"""

from pydantic import Field, field_validator

from app.dto.common import CamelModel, UtcDatetime

# ── 请求体 ──────────────────────────────────────


class RoomCreate(CamelModel):
    """POST /api/v1/rooms 请求体"""

    nickname: str | None = Field(default=None, max_length=100)
    room_name: str = Field(..., min_length=1, max_length=200)
    max_players: int = Field(default=4, ge=1, le=20)

    @field_validator("nickname", "room_name")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("不能为空")
        return stripped


class SelectModuleBody(CamelModel):
    """POST /api/v1/rooms/{roomId}/module 请求体"""

    module_id: str = Field(..., min_length=1)
    attribute_gen_method: str = Field(default="point_buy")


class JoinRoomBody(CamelModel):
    """POST /api/v1/rooms/{roomCode}/join 请求体"""

    nickname: str | None = Field(default=None, max_length=100)

    @field_validator("nickname")
    @classmethod
    def strip_nickname(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("昵称不能为空")
        return stripped


# ── 响应体 ──────────────────────────────────────


class RoomCreateResult(CamelModel):
    """POST /api/v1/rooms 返回"""

    room_id: str
    room_code: str
    reconnect_token: str
    player_id: str
    # 🔴 我在这个房间里是不是房主。**必填、由服务端给**：在此之前前端是自己
    # 猜的——建房那条路写死 true、加入那两条路写死 false，于是「换设备重进」
    # 和「转让房主之后」两种情况下真房主拿到的是 false，开始游戏的按钮根本
    # 不显示。房主身份在 `players.is_host` 上，服务端本来就知道答案。
    is_host: bool
    # 这个房间里属于我的角色卡 id，还没建卡时为 None。
    #
    # 加它是为了让**换设备重连**真正可用（PR #110 review [1]）：客户端靠它才知道
    # 该去拉哪张卡，而在此之前这个 id 只在建卡那一刻由客户端自己存着——换台设备
    # 就永远拿不回来，已经建完卡的人重连后会显示成"还没建卡"、被引导去建第二张。
    character_id: str | None = None


class RoomPlayerRead(CamelModel):
    """房间内玩家摘要。

    注意 `player_id` 对应 ORM `Player` 的主键属性 `id`（名字不一样），所以不能直接
    `model_validate(player_orm)`——调用方需要显式映射 `player_id=p.id`（见
    service/room.py 的 _to_room_preview）。`from_attributes=True` 仍保留，方便
    其余名字一致的字段。camelCase 别名生成、populate_by_name 继承自 `CamelModel`——
    pydantic 的 `model_config` 在子类里是合并而非整体覆盖父类配置，这里不需要
    重复声明（issue #77 审计发现 #1，原先这里重写了一份和父类一样的配置，是
    #75 遗留的死代码）。
    """

    model_config = {"from_attributes": True}
    player_id: str
    nickname: str
    is_host: bool
    ready: bool
    has_character: bool
    # AI 队友（exec/21）。前端要能把它跟真人区分开——玩家有权知道桌上哪个
    # 是补位的，这不是该藏起来的信息。
    is_ai: bool = False


class TransferHostBody(CamelModel):
    """POST /api/v1/rooms/{roomId}/host 请求体——把房主交给谁。"""

    player_id: str = Field(..., min_length=1)


class PlayerAwayBody(CamelModel):
    """POST /api/v1/rooms/{roomId}/players/{playerId}/away 请求体。

    显式的 `away` 而不是两个动词端点（`/away` 与 `/back`）：**这是一个开关，
    不是两件事**，两个端点会让"他到底在不在"多出一处需要同步的判断。
    """

    away: bool


class RoomSettingsBody(CamelModel):
    """PATCH /api/v1/rooms/{roomId} 请求体。

    人数上限 + 「骰子在桌上」。房间名不在这里：改名是纯展示需求，而这条接口
    的存在理由是"位置不够了"这个会卡住桌子的问题——两件事没必要绑在一起。
    区间跟建房时一致（`RoomCreate.max_players`），下界由服务层再按当前人数
    收紧一次（不能调到比在座的人还少）。

    🔴 `allow_manual_rolls` **可选**：不传 = 不动它。这条接口原本只改人数，
    把它做成必填会让所有既有调用方（前端那一处、e2e）在不知情的情况下把开关
    重置成 False——**加字段时给已有调用方留原样不动的那条路**。
    """

    max_players: int = Field(..., ge=1, le=20)
    #: 「骰子在桌上」（`exec/46` B5）。`None` = 保持不变。
    allow_manual_rolls: bool | None = None


class AiPlayerCreateBody(CamelModel):
    """加一个 AI 队友（exec/21）。三个字段都可选。

    `seed` 用于可复现——同一个 seed 造出同一张卡，测试与试玩装置需要它。
    """

    nickname: str | None = None
    occupation: str | None = None
    seed: int | None = None


class ModuleRead(CamelModel):
    """模组信息（对应内容库 `Scenario` 表，`from_attributes=True` 支持直接从
    ORM 对象构造）。"""

    model_config = {"from_attributes": True}
    id: str
    title: str
    version: str
    authors: list[str]
    players_min: int
    players_max: int
    difficulty: int
    estimated_duration: str | None = None
    #: 这个模组是不是导入进来的。前端靠它区分「内置」与「我导入的」——两者能给的
    #: 信息不一样：内置有人工填的难度与简介，导入的只有导入日期与规模。
    #: 由 `owner_user_id` 推出（内置无主），不是另一份状态。
    is_imported: bool = False
    created_at: UtcDatetime | None = None


class RoomPreview(CamelModel):
    """GET /api/v1/rooms/{roomCode} 返回"""

    room_id: str
    room_code: str
    room_name: str
    phase: str
    story_started: bool
    module_id: str | None = None
    module_title: str | None = None
    player_count: int
    max_players: int
    #: 「骰子在桌上」（`exec/46` B5）。前端靠它决定掷骰卡片给不给"我自己掷的"
    #: 那个入口。**不给默认值**：服务端每次都送得出，契约就该说它一定在
    #: （给了默认值 = 生成的 TS 可选 = 前端只能 `?? false`，这个仓库一天踩过两次）。
    allow_manual_rolls: bool
    players: list[RoomPlayerRead]


class LastSessionRead(CamelModel):
    """GET /api/v1/rooms/{roomId}/last-session 返回：「上次讲到哪」（`exec/46` B3）。

    🔴 `recap_text` **必填但可为 null**，不给默认值：服务端每次都送得出这个
    字段，`null` 的含义是「这一局还没散过会 / 那一场什么都没发生 / 没配 key」
    ——三种都是如实降级。给了默认值生成的 TS 就是可选的，前端只能 `?? ''`，
    而那正好把「没有」和「没送」混成一件事（这个仓库一天踩过两次）。
    """

    #: 这一局到现在聚过几次。第一次开局就是 1。
    session_count: int
    #: 上一场的「上次讲到哪」。没有就是 null，**别拿占位文案填**。
    recap_text: str | None
    #: 现在是不是收工状态（房主按了「今晚到此为止」）。
    adjourned: bool


class MyRoomSummary(CamelModel):
    """GET /api/v1/me/rooms 返回项"""

    room_id: str
    room_code: str
    room_name: str
    phase: str
    module_id: str | None = None
    module_title: str | None = None
    player_count: int
    max_players: int
    updated_at: UtcDatetime
    #: 当前账号是不是这个房间的房主（删除房间是房主专属操作）。
    is_host: bool = False
