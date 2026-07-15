"""Room 模块的 pydantic 请求/响应模型。

与 trpg-sdk/src/types.ts 手动保持同步。

命名约定：
- 后端代码内统一使用 snake_case Python 命名
- 通过 alias_generator 实现 JSON 层的 camelCase ↔ snake_case 自动映射
- 请求（camelCase JSON → snake_case Python）和响应（snake_case Python → camelCase JSON）
  由 pydantic 自动处理，业务代码无需关心
"""

from datetime import datetime

from pydantic import AliasGenerator, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


def _to_camel(snake: str) -> str:
    return to_camel(snake)


class CamelModel(BaseModel):
    """所有 Room DTO 的基类：JSON 层使用 camelCase，Python 层使用 snake_case。"""
    model_config = ConfigDict(
        alias_generator=AliasGenerator(alias=_to_camel),
        populate_by_name=True,
    )


# ── 请求体 ──────────────────────────────────────

class RoomCreate(CamelModel):
    """POST /api/v1/rooms 请求体"""
    nickname: str | None = Field(default=None, max_length=100)
    room_name: str = Field(..., min_length=1, max_length=200)
    max_players: int = Field(default=4, ge=1, le=20)


class SelectModuleBody(CamelModel):
    """POST /api/v1/rooms/{roomId}/module 请求体"""
    module_id: str = Field(..., min_length=1)
    attribute_gen_method: str = Field(default="point_buy")


class JoinRoomBody(CamelModel):
    """POST /api/v1/rooms/{roomCode}/join 请求体"""
    nickname: str | None = Field(default=None, max_length=100)


# ── 响应体 ──────────────────────────────────────

class RoomCreateResult(CamelModel):
    """POST /api/v1/rooms 返回"""
    room_id: str
    room_code: str
    reconnect_token: str
    player_id: str


class RoomPlayerRead(CamelModel):
    """房间内玩家摘要"""
    model_config = ConfigDict(
        alias_generator=AliasGenerator(alias=_to_camel),
        populate_by_name=True,
        from_attributes=True,
    )
    player_id: str
    nickname: str
    is_host: bool
    ready: bool
    has_character: bool


class ModuleRead(CamelModel):
    """模组信息"""
    id: str
    title: str
    version: str
    authors: list[str]
    players_min: int
    players_max: int
    difficulty: int
    estimated_duration: str | None = None


class RoomPreview(CamelModel):
    """GET /api/v1/rooms/{roomCode} 返回"""
    room_id: str
    room_code: str
    room_name: str
    phase: str
    story_started: bool
    module_title: str | None = None
    player_count: int
    max_players: int
    players: list[RoomPlayerRead]


class MyRoomSummary(CamelModel):
    """GET /api/v1/me/rooms 返回项"""
    room_id: str
    room_code: str
    room_name: str
    phase: str
    module_title: str | None = None
    player_count: int
    max_players: int
    updated_at: datetime
