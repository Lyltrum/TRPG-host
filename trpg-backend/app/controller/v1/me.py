"""Controller 层：`/api/v1/me` 路由 —— 当前用户相关接口。"""

from fastapi import APIRouter, Header, status

from app.core.errors import AppException, ErrorCode
from app.dto.common import ApiResponse
from app.dto.room import MyRoomSummary
from app.service import room as room_service

router = APIRouter(prefix="/me", tags=["me"])


@router.get("/rooms", response_model=ApiResponse[list[MyRoomSummary]])
async def list_my_rooms(
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
) -> ApiResponse[list[MyRoomSummary]]:
    """GET /api/v1/me/rooms —— 获取我的房间列表。"""
    try:
        rooms = await room_service.list_my_rooms(reconnect_token)
    except room_service.RoomAuthenticationError as exc:
        raise AppException(ErrorCode.UNAUTHORIZED, str(exc), status.HTTP_401_UNAUTHORIZED) from exc
    return ApiResponse.ok(rooms)
