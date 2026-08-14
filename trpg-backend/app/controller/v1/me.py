"""Controller 层：`/api/v1/me` 路由 —— 当前用户相关接口。

「我的常用角色卡库」那 4 个端点铺于 issue #77（决策 5），当时是 NOT_IMPLEMENTED
桩；2026-08-13 接上真实读写。
"""

from typing import NoReturn

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.controller.dependencies import extract_bearer_token, get_current_user
from app.core.db import get_db
from app.core.errors import AppException, ErrorCode
from app.dto.character import (
    CharacterTemplateCreateBody,
    CharacterTemplateOverwriteBody,
    CharacterTemplateRead,
    CharacterTemplateUpdateBody,
)
from app.dto.common import ApiResponse
from app.dto.room import MyRoomSummary
from app.models.user import User
from app.service import auth as auth_service
from app.service import character as character_service
from app.service import room as room_service

router = APIRouter(prefix="/me", tags=["me"])


def _raise_service_error(exc: Exception) -> NoReturn:
    """把 service 层的领域异常翻成 HTTP 错误。

    🔴 这一层此前**不存在**——那时四个端点全是 NOT_IMPLEMENTED（本身就是
    AppException，直接穿过去）。接上真实读写的同一刻，没有翻译层的 ValueError
    就会变成 500：「加一道门，必须同时给它配一条走得通的修法」。
    """
    if isinstance(
        exc,
        character_service.CharacterTemplateNotFoundError | character_service.CharacterNotFoundError,
    ):
        raise AppException(ErrorCode.NOT_FOUND, str(exc), status.HTTP_404_NOT_FOUND) from exc
    if isinstance(exc, character_service.CharacterTemplateNotEditableError):
        raise AppException(
            ErrorCode.VALIDATION_ERROR, str(exc), status.HTTP_422_UNPROCESSABLE_CONTENT
        ) from exc
    raise exc


@router.get("/rooms", response_model=ApiResponse[list[MyRoomSummary]])
async def list_my_rooms(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApiResponse[list[MyRoomSummary]]:
    """GET /api/v1/me/rooms —— 获取我的房间列表。

    issue #106：凭证从房间的 `X-Reconnect-Token` 换成账号 `Authorization`。原来
    按重连凭证查，一个凭证只对应一名玩家/一个房间，「我的游戏」实际上是「这个
    浏览器的最后一个房间」——换台设备就什么都看不到，而账号体系当初正是为
    「换设备找回游戏」引入的。
    """
    rooms = await room_service.list_my_rooms(db, user)
    return ApiResponse.ok(rooms)


async def _require_user_id(authorization: str | None, db: AsyncSession) -> str:
    try:
        me = await auth_service.get_me(db, extract_bearer_token(authorization))
    except auth_service.AuthenticationError as exc:
        raise AppException(ErrorCode.UNAUTHORIZED, str(exc), status.HTTP_401_UNAUTHORIZED) from exc
    return me.user_id


@router.get("/character-templates", response_model=ApiResponse[list[CharacterTemplateRead]])
async def list_character_templates(
    authorization: str | None = Header(default=None), db: AsyncSession = Depends(get_db)
) -> ApiResponse[list[CharacterTemplateRead]]:
    """GET /api/v1/me/character-templates —— 我的卡库列表。"""
    user_id = await _require_user_id(authorization, db)
    templates = await character_service.list_character_templates(db, user_id)
    return ApiResponse.ok(templates)


@router.post(
    "/character-templates",
    response_model=ApiResponse[CharacterTemplateRead],
    status_code=status.HTTP_201_CREATED,
)
async def create_character_template(
    payload: CharacterTemplateCreateBody,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[CharacterTemplateRead]:
    """POST /api/v1/me/character-templates —— 把一张角色卡保存为常用卡。"""
    user_id = await _require_user_id(authorization, db)
    try:
        template = await character_service.create_character_template(db, user_id, payload)
    except ValueError as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(template)


@router.get("/character-templates/{template_id}", response_model=ApiResponse[CharacterTemplateRead])
async def get_character_template(
    template_id: str,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[CharacterTemplateRead]:
    """GET /api/v1/me/character-templates/{templateId} —— 卡库详情。"""
    user_id = await _require_user_id(authorization, db)
    try:
        template = await character_service.get_character_template(db, user_id, template_id)
    except ValueError as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(template)


@router.put("/character-templates/{template_id}", response_model=ApiResponse[CharacterTemplateRead])
async def overwrite_character_template(
    template_id: str,
    payload: CharacterTemplateOverwriteBody,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[CharacterTemplateRead]:
    """PUT /api/v1/me/character-templates/{templateId} —— 用一张角色卡的当前状态
    整份覆盖卡库里那张（「改完了，更新我卡库里那张」）。"""
    user_id = await _require_user_id(authorization, db)
    try:
        template = await character_service.overwrite_character_template(
            db, user_id, template_id, payload
        )
    except ValueError as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(template)


@router.patch(
    "/character-templates/{template_id}", response_model=ApiResponse[CharacterTemplateRead]
)
async def update_character_template(
    template_id: str,
    payload: CharacterTemplateUpdateBody,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[CharacterTemplateRead]:
    """PATCH /api/v1/me/character-templates/{templateId} —— 改卡库里那张卡的文字。"""
    user_id = await _require_user_id(authorization, db)
    try:
        template = await character_service.update_character_template(
            db, user_id, template_id, payload
        )
    except ValueError as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(template)


@router.delete("/character-templates/{template_id}", response_model=ApiResponse[None])
async def delete_character_template(
    template_id: str,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[None]:
    """DELETE /api/v1/me/character-templates/{templateId} —— 删除常用卡。"""
    user_id = await _require_user_id(authorization, db)
    try:
        await character_service.delete_character_template(db, user_id, template_id)
    except ValueError as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(None)
