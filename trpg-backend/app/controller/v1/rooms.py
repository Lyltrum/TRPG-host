"""Controller 层：`/api/v1/rooms` 路由 —— 房间 CRUD 和生命周期管理（issue #77
切换为真实数据库读写 + 补充 roll-attributes / summary / replay 3 个新端点）。

角色卡相关路由（挂在 `/rooms/{roomId}/characters` 下）本期改为调用独立的
`service/character.py`，不再是 `room_service` 的一部分（issue #77 决策：
`auth`/`room`/`character`/`ws` 四个 service 各自独立）。
"""

from typing import NoReturn

from fastapi import APIRouter, Body, Depends, Header, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.controller.dependencies import get_current_user
from app.core.db import get_db
from app.core.errors import AppException, ErrorCode
from app.dto.character import (
    AgeAdjustmentRequest,
    AgeAdjustmentResult,
    CharacterCreateBody,
    CharacterDraftResult,
    CharacterRead,
    CharacterUpdateBody,
    PartyCharacterRead,
    QuickBuildCharacterBody,
    RollAttributePoolResult,
    RollAttributesResult,
    RollLuckResult,
)
from app.dto.chat import ChatMessageRead
from app.dto.common import ApiResponse
from app.dto.replay import ReplayEventRead, RoomSummaryRead
from app.dto.room import (
    AiPlayerCreateBody,
    JoinRoomBody,
    PlayerAwayBody,
    RoomCreate,
    RoomCreateResult,
    RoomPlayerRead,
    RoomPreview,
    RoomSettingsBody,
    SelectModuleBody,
    TransferHostBody,
)
from app.models.user import User
from app.service import ai_player as ai_player_service
from app.service import character as character_service
from app.service import chat as chat_service
from app.service import room as room_service

router = APIRouter(prefix="/rooms", tags=["rooms"])

# 房间域的异常 → (错误码, HTTP 状态码) 映射表。issue #77 决策 2：业务语义层的
# 错误码以架构文档为准，这里把"房间不存在"/"房间已满"/"未选模组"这些原来共用
# 通用 NOT_FOUND/CONFLICT 的场景拆成更具体的业务码；找不到映射的异常兜底成
# NOT_FOUND，保持原来的行为（角色/模组这类没有专属业务码的"找不到"场景）。
_ERROR_MAP: dict[type[Exception], tuple[ErrorCode, int]] = {
    room_service.RoomNotFoundError: (ErrorCode.ROOM_NOT_FOUND, status.HTTP_404_NOT_FOUND),
    room_service.ModuleNotFoundError: (ErrorCode.NOT_FOUND, status.HTTP_404_NOT_FOUND),
    room_service.RoomFullError: (ErrorCode.ROOM_FULL, status.HTTP_409_CONFLICT),
    room_service.ModuleNotSelectedError: (ErrorCode.MODULE_NOT_SELECTED, status.HTTP_409_CONFLICT),
    room_service.CharacterIncompleteError: (
        ErrorCode.CHARACTER_INCOMPLETE,
        status.HTTP_409_CONFLICT,
    ),
    room_service.RulesetNotConfiguredError: (
        ErrorCode.RULESET_NOT_CONFIGURED,
        status.HTTP_409_CONFLICT,
    ),
    room_service.RoomConflictError: (ErrorCode.CONFLICT, status.HTTP_409_CONFLICT),
    room_service.RoomAuthenticationError: (ErrorCode.UNAUTHORIZED, status.HTTP_401_UNAUTHORIZED),
    room_service.RoomAuthorizationError: (ErrorCode.FORBIDDEN, status.HTTP_403_FORBIDDEN),
    character_service.CharacterNotFoundError: (ErrorCode.NOT_FOUND, status.HTTP_404_NOT_FOUND),
    character_service.AttributesNotSetError: (
        ErrorCode.ATTRIBUTES_NOT_SET,
        status.HTTP_409_CONFLICT,
    ),
    character_service.AlreadyRolledError: (ErrorCode.ALREADY_ROLLED, status.HTTP_409_CONFLICT),
    character_service.CharacterTemplateNotFoundError: (
        ErrorCode.NOT_FOUND,
        status.HTTP_404_NOT_FOUND,
    ),
}


def _raise_service_error(exc: Exception) -> NoReturn:
    for exc_type, (code, http_status) in _ERROR_MAP.items():
        if isinstance(exc, exc_type):
            raise AppException(code, str(exc), http_status) from exc
    raise AppException(ErrorCode.NOT_FOUND, str(exc), status.HTTP_404_NOT_FOUND) from exc


@router.post("", response_model=ApiResponse[RoomCreateResult])
async def create_room(
    payload: RoomCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApiResponse[RoomCreateResult]:
    """POST /api/v1/rooms —— 创建房间，返回房间身份信息。

    issue #106 起要求登录：房间和房主玩家都要关联到真实账号，否则
    `host_user_id`/`user_id` 永远是空的，「我的游戏」和跨设备找回都无从谈起。
    """
    result = await room_service.create_room(db, payload, user)
    return ApiResponse.ok(result)


@router.post("/{room_id}/module", response_model=ApiResponse[None])
async def select_room_module(
    room_id: str,
    payload: SelectModuleBody,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[None]:
    """POST /api/v1/rooms/{roomId}/module —— 房主选定模组。"""
    try:
        await room_service.select_module(db, room_id, payload, reconnect_token)
    except (
        room_service.RoomNotFoundError,
        room_service.ModuleNotFoundError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
        room_service.RoomConflictError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(None)


@router.post("/{room_code}/join", response_model=ApiResponse[RoomCreateResult])
async def join_room(
    room_code: str,
    payload: JoinRoomBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApiResponse[RoomCreateResult]:
    """POST /api/v1/rooms/{roomCode}/join —— 用房间码加入房间。

    issue #106 起要求登录，且**已是房间成员时幂等返回既有身份**（用于掉线重连）。
    """
    try:
        result = await room_service.join_room(db, room_code, payload, user)
    except (
        room_service.RoomNotFoundError,
        room_service.RoomFullError,
        room_service.RoomConflictError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(result)


@router.get("/{room_code}", response_model=ApiResponse[RoomPreview])
async def get_room_info(
    room_code: str, db: AsyncSession = Depends(get_db)
) -> ApiResponse[RoomPreview]:
    """GET /api/v1/rooms/{roomCode} —— 获取房间信息 + 玩家列表。"""
    preview = await room_service.get_room_preview(db, room_code)
    if preview is None:
        raise AppException(ErrorCode.ROOM_NOT_FOUND, "房间不存在", status.HTTP_404_NOT_FOUND)
    return ApiResponse.ok(preview)


@router.post("/{room_id}/ai-players", response_model=ApiResponse[RoomPlayerRead], status_code=201)
async def add_ai_player(
    room_id: str,
    request: Request,
    body: AiPlayerCreateBody | None = None,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[RoomPlayerRead]:
    """POST /api/v1/rooms/{roomId}/ai-players —— 房主加一个 AI 队友（exec/21）。

    人不齐时补位。它有一张**规则上合法**的完成态角色卡（`service/ai_player.py`
    按 COC7 规则生成并自检），因此天然满足"全员建卡完成"这个开局条件。

    本期它**不会说话**：占座位、进名单、算进位置分组、卡可被队友传阅，但不提交
    行动——行动决策是 exec/21 第三层。
    """
    payload = body or AiPlayerCreateBody()
    try:
        player = await ai_player_service.add_ai_player_to_room(
            db,
            room_id,
            reconnect_token,
            nickname=payload.nickname,
            occupation_name=payload.occupation,
            seed=payload.seed,
            writer=getattr(request.app.state, "background_writer", None),
        )
    except room_service.RoomNotFoundError as exc:
        raise AppException(ErrorCode.ROOM_NOT_FOUND, str(exc), status.HTTP_404_NOT_FOUND) from exc
    except (room_service.RoomAuthenticationError, room_service.RoomAuthorizationError) as exc:
        raise AppException(ErrorCode.FORBIDDEN, str(exc), status.HTTP_403_FORBIDDEN) from exc
    except room_service.RoomConflictError as exc:
        raise AppException(ErrorCode.CONFLICT, str(exc), status.HTTP_409_CONFLICT) from exc
    except ValueError as exc:  # 未知职业
        raise AppException(ErrorCode.BAD_REQUEST, str(exc), status.HTTP_400_BAD_REQUEST) from exc
    return ApiResponse(
        success=True,
        data=RoomPlayerRead(
            player_id=player.id,
            nickname=player.nickname,
            is_host=player.is_host,
            ready=player.ready,
            has_character=player.has_character,
            is_ai=player.is_ai,
        ),
    )


@router.post("/{room_id}/start-story", response_model=ApiResponse[None])
async def start_story(
    room_id: str,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[None]:
    """POST /api/v1/rooms/{roomId}/start-story —— 房主开始游戏。"""
    try:
        await room_service.start_story(db, room_id, reconnect_token)
    except (
        room_service.RoomNotFoundError,
        room_service.ModuleNotSelectedError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
        room_service.RoomConflictError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(None)


@router.post("/{room_id}/end", response_model=ApiResponse[None])
async def end_game(
    room_id: str,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[None]:
    """POST /api/v1/rooms/{roomId}/end —— 房主结束游戏。"""
    try:
        await room_service.end_game(db, room_id, reconnect_token)
    except (
        room_service.RoomNotFoundError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
        room_service.RoomConflictError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(None)


@router.delete("/{room_id}/players/{player_id}", response_model=ApiResponse[None])
async def kick_player(
    room_id: str,
    player_id: str,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[None]:
    """DELETE /api/v1/rooms/{roomId}/players/{playerId} —— 房主在大厅移出玩家。"""
    try:
        await room_service.kick_player(db, room_id, player_id, reconnect_token)
    except (
        room_service.RoomNotFoundError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
        room_service.RoomConflictError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(None)


@router.post("/{room_id}/host", response_model=ApiResponse[None])
async def transfer_host(
    room_id: str,
    payload: TransferHostBody,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[None]:
    """POST /api/v1/rooms/{roomId}/host —— 转让房主。"""
    try:
        await room_service.transfer_host(db, room_id, payload.player_id, reconnect_token)
    except (
        room_service.RoomNotFoundError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
        room_service.RoomConflictError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(None)


@router.patch("/{room_id}", response_model=ApiResponse[None])
async def update_room_settings(
    room_id: str,
    payload: RoomSettingsBody,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[None]:
    """PATCH /api/v1/rooms/{roomId} —— 改人数上限。"""
    try:
        await room_service.update_room_settings(db, room_id, payload.max_players, reconnect_token)
    except (
        room_service.RoomNotFoundError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
        room_service.RoomConflictError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(None)


@router.post("/{room_id}/players/{player_id}/away", response_model=ApiResponse[None])
async def set_player_away(
    room_id: str,
    player_id: str,
    payload: PlayerAwayBody,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[None]:
    """POST /api/v1/rooms/{roomId}/players/{playerId}/away —— 中途离开 / 回来。

    `away=true` 让这个角色暂时退出剧情（守秘人下一段会把他圆出去），
    `away=false` 是回来。本人或房主可操作。
    """
    try:
        await room_service.set_player_away(db, room_id, player_id, payload.away, reconnect_token)
    except (
        room_service.RoomNotFoundError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
        room_service.RoomConflictError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(None)


@router.post("/{room_id}/disband", response_model=ApiResponse[None])
async def disband_room(
    room_id: str,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[None]:
    """POST /api/v1/rooms/{roomId}/disband —— 房主解散房间。"""
    try:
        await room_service.disband_room(db, room_id, reconnect_token)
    except (
        room_service.RoomNotFoundError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
        room_service.RoomConflictError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(None)


@router.get("/{room_id}/summary", response_model=ApiResponse[RoomSummaryRead])
async def get_room_summary(
    room_id: str, db: AsyncSession = Depends(get_db)
) -> ApiResponse[RoomSummaryRead]:
    """GET /api/v1/rooms/{roomId}/summary —— 复盘摘要。

    上半是代码算的数字，下半是模型写的一段回顾（没配 key 时为 null，
    见 `service/recap.py`）。
    """
    try:
        summary = await room_service.get_summary(db, room_id)
    except room_service.RoomNotFoundError as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(summary)


@router.get("/{room_id}/replay", response_model=ApiResponse[list[ReplayEventRead]])
async def get_room_replay(
    room_id: str,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[ReplayEventRead]]:
    """GET /api/v1/rooms/{roomId}/replay —— 逐条事件回放（仅本房间成员可查）。"""
    try:
        events = await room_service.get_replay(db, room_id, reconnect_token)
    except (
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(events)


@router.get("/{room_id}/messages", response_model=ApiResponse[list[ChatMessageRead]])
async def list_room_messages(
    room_id: str,
    before: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[ChatMessageRead]]:
    """GET /api/v1/rooms/{roomId}/messages —— 讨论区历史消息，倒序分页
    （issue #107）。

    刷新页面/断线重连后靠它拉回聊天历史（实时消息走 WS 的 chat.message
    广播）。鉴权同 replay：讨论区内容只有本房间成员能看——roomId 会被公开
    房间预览暴露，不能凭 roomId 白拿。`before` 传上一页最后一条的
    messageId 继续往前翻。
    """
    try:
        # 只为鉴权，成员身份本身不参与查询
        await room_service.require_room_member(db, room_id, reconnect_token)
    except (
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
    ) as exc:
        _raise_service_error(exc)
    messages = await chat_service.list_chat_messages(db, room_id, before, limit)
    return ApiResponse.ok(messages)


# ── 角色（issue #59，本期切到 service/character.py） ──────────────────────────


@router.post(
    "/{room_id}/characters",
    response_model=ApiResponse[CharacterDraftResult],
    status_code=status.HTTP_201_CREATED,
    tags=["characters"],
)
async def create_character(
    room_id: str,
    payload: CharacterCreateBody | None = Body(default=None),
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[CharacterDraftResult]:
    """POST /api/v1/rooms/{roomId}/characters —— 玩家创建一份角色草稿。

    `basedOnTemplateId`（第三条建卡路径）：复用自己的常用卡，把建卡态整份
    复制进新草稿。
    """
    based_on_template_id = payload.based_on_template_id if payload else None
    try:
        result = await character_service.create_character_draft(
            db, room_id, reconnect_token, based_on_template_id
        )
    except (
        room_service.RoomNotFoundError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
        character_service.CharacterTemplateNotFoundError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(result)


@router.post(
    "/{room_id}/characters/quick-build",
    response_model=ApiResponse[CharacterDraftResult],
    status_code=status.HTTP_201_CREATED,
    tags=["characters"],
)
async def quick_build_character(
    room_id: str,
    payload: QuickBuildCharacterBody,
    request: Request,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[CharacterDraftResult]:
    """POST /api/v1/rooms/{roomId}/characters/quick-build —— 一键生成一张合法角色卡。

    给零基础玩家的第二条建卡路径（真人实测反馈：八步向导对新人不友好）。
    生成器与 AI 队友共用同一个，产出的卡是 `complete` 状态，直接可以开局。
    想自己捏的人走原来的向导，两条路互不影响。
    """
    try:
        result = await character_service.quick_build_character(
            db,
            room_id,
            reconnect_token,
            payload.name,
            writer=getattr(request.app.state, "background_writer", None),
        )
    except character_service.CharacterInvalidError as exc:
        raise AppException(
            ErrorCode.CHARACTER_INVALID,
            str(exc),
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            details=[
                {"code": issue.code, "field": issue.field, "message": issue.message}
                for issue in exc.issues
            ],
        ) from exc
    except (
        room_service.RoomNotFoundError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(result)


@router.get(
    "/{room_id}/characters",
    response_model=ApiResponse[list[PartyCharacterRead]],
    tags=["characters"],
)
async def list_party_characters(
    room_id: str,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[PartyCharacterRead]]:
    """GET /api/v1/rooms/{roomId}/characters —— 看队友的角色卡（exec/14 P5.3）。

    与下面「读回自己那张」的区别是鉴权口径：这里只要求**你是房间里的人**。
    真人桌上角色卡互相传阅，此前系统只能读自己那张，比真人桌还封闭
    （exec/18 ⑨）。⑦⑧ 已裁决检定与 HP/SAN 公开，故不做脱敏。
    """
    try:
        party = await character_service.list_party_characters(db, room_id, reconnect_token)
    except (
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(party)


@router.get(
    "/{room_id}/characters/{character_id}",
    response_model=ApiResponse[CharacterRead],
    tags=["characters"],
)
async def get_character(
    room_id: str,
    character_id: str,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[CharacterRead]:
    """GET /api/v1/rooms/{roomId}/characters/{characterId} —— 读回自己的角色卡
    （issue #96）。

    补这个端点是为了让后端成为角色卡的唯一事实来源，客户端不必再把角色卡
    存进本地当权威源（那份副本会随后端 schema 演进而过期）。
    """
    try:
        character = await character_service.get_character(
            db, room_id, character_id, reconnect_token
        )
    except (
        character_service.CharacterNotFoundError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(character)


@router.post(
    "/{room_id}/characters/{character_id}/regenerate-background",
    response_model=ApiResponse[CharacterRead],
    tags=["characters"],
)
async def regenerate_background(
    room_id: str,
    character_id: str,
    request: Request,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[CharacterRead]:
    """POST /api/v1/rooms/{roomId}/characters/{characterId}/regenerate-background

    重摇一次角色背景（exec/25 P1 #5）。只换过去，属性/技能/职业一个都不动。

    `exec/20 §1.9` 定的方向：内容质量不该由代码判，该给玩家一个重摇的按钮。
    """
    try:
        character = await character_service.regenerate_background(
            db,
            room_id,
            character_id,
            reconnect_token,
            writer=getattr(request.app.state, "background_writer", None),
        )
    except character_service.BackgroundUnavailableError as exc:
        # 🔴 显式失败而不是静默保持原样：玩家主动点了「换一个」，没反应会让他
        # 以为按钮坏了然后一直点。503 = 依赖的外部服务这会儿不可用。
        raise AppException(
            ErrorCode.INTERNAL_ERROR,
            "背景生成服务暂时不可用，请稍后再试",
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ) from exc
    except (
        character_service.CharacterNotFoundError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(character)


@router.patch(
    "/{room_id}/characters/{character_id}",
    response_model=ApiResponse[None],
    tags=["characters"],
)
async def update_character(
    room_id: str,
    character_id: str,
    payload: CharacterUpdateBody,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[None]:
    """PATCH /api/v1/rooms/{roomId}/characters/{characterId} —— 保存建卡向导算好的完整角色数据。"""
    try:
        await character_service.update_character(
            db, room_id, character_id, payload, reconnect_token
        )
    except (
        character_service.CharacterNotFoundError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(None)


@router.post(
    "/{room_id}/characters/{character_id}/complete",
    response_model=ApiResponse[None],
    tags=["characters"],
)
async def complete_character(
    room_id: str,
    character_id: str,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[None]:
    """POST /api/v1/rooms/{roomId}/characters/{characterId}/complete —— 标记建卡完成。

    issue #84 S2：落库前的权威校验没通过时，`character_service.complete_character`
    抛 `CharacterInvalidError`，这里转成 422 + 结构化校验报告（`AppException.details`），
    不走 `_raise_service_error`（那条路径只有 code/message，装不下校验报告）。
    """
    try:
        await character_service.complete_character(db, room_id, character_id, reconnect_token)
    except character_service.CharacterInvalidError as exc:
        raise AppException(
            ErrorCode.CHARACTER_INVALID,
            str(exc),
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            details=[
                {"code": issue.code, "field": issue.field, "message": issue.message}
                for issue in exc.issues
            ],
        ) from exc
    except (
        character_service.CharacterNotFoundError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
        room_service.RulesetNotConfiguredError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(None)


@router.post(
    "/{room_id}/characters/{character_id}/roll-attributes",
    response_model=ApiResponse[RollAttributesResult],
    tags=["characters"],
)
async def roll_attributes(
    room_id: str,
    character_id: str,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[RollAttributesResult]:
    """POST /api/v1/rooms/{roomId}/characters/{characterId}/roll-attributes ——
    服务端权威掷骰生成属性（issue #77 新增，真实实现，不是 NOT_IMPLEMENTED 桩）。
    """
    try:
        result = await character_service.roll_attributes(db, room_id, character_id, reconnect_token)
    except (
        character_service.CharacterNotFoundError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(result)


@router.post(
    "/{room_id}/characters/{character_id}/roll-attribute-pool",
    response_model=ApiResponse[RollAttributePoolResult],
    tags=["characters"],
)
async def roll_attribute_pool(
    room_id: str,
    character_id: str,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[RollAttributePoolResult]:
    """POST /api/v1/rooms/{roomId}/characters/{characterId}/roll-attribute-pool
    —— 掷点池生成法：服务端权威掷出一个总点数池，玩家再手动分配到八维
    （迁移自 coc-char-gen，见 docs/character-build-migration/design.md）。
    """
    try:
        result = await character_service.roll_attribute_pool(
            db, room_id, character_id, reconnect_token
        )
    except (
        character_service.CharacterNotFoundError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
        character_service.AlreadyRolledError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(result)


@router.post(
    "/{room_id}/characters/{character_id}/roll-luck",
    response_model=ApiResponse[RollLuckResult],
    tags=["characters"],
)
async def roll_luck(
    room_id: str,
    character_id: str,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[RollLuckResult]:
    """POST /api/v1/rooms/{roomId}/characters/{characterId}/roll-luck ——
    幸运单掷（character-build-migration redesign-v2 §4-A，真实实现，不是
    NOT_IMPLEMENTED 桩）：独立于属性生成方式，点数购买/掷骰/掷点池三种
    生成法的玩家都能调这个端点掷幸运。
    """
    try:
        result = await character_service.roll_luck(db, room_id, character_id, reconnect_token)
    except (
        character_service.CharacterNotFoundError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
        character_service.AlreadyRolledError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(result)


@router.post(
    "/{room_id}/characters/{character_id}/apply-age-adjustment",
    response_model=ApiResponse[AgeAdjustmentResult],
    tags=["characters"],
)
async def apply_age_adjustment(
    room_id: str,
    character_id: str,
    payload: AgeAdjustmentRequest,
    reconnect_token: str | None = Header(default=None, alias="X-Reconnect-Token"),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[AgeAdjustmentResult]:
    """POST /api/v1/rooms/{roomId}/characters/{characterId}/apply-age-adjustment
    —— 套用 COC7 建卡期年龄修正（EDU 改进检定/身体减值/外貌减值/青年幸运
    双掷），迁移自 coc-char-gen。必须先生成过属性，否则 409
    `ATTRIBUTES_NOT_SET`。
    """
    try:
        result = await character_service.apply_age_adjustment(
            db, room_id, character_id, payload.age, reconnect_token
        )
    except (
        character_service.CharacterNotFoundError,
        character_service.AttributesNotSetError,
        room_service.RoomAuthenticationError,
        room_service.RoomAuthorizationError,
    ) as exc:
        _raise_service_error(exc)
    return ApiResponse.ok(result)
