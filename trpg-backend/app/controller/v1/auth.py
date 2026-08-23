"""Controller 层：`/api/v1/auth` 路由 —— 注册/登录/登出/当前用户（issue #58，
issue #77 切换为真实数据库读写）。

登录凭证通过标准的 `Authorization: Bearer <token>` 请求头传递，跟 rooms 模块
自定义的 `X-Reconnect-Token` 是两套独立的身份体系：账号会话认的是"这是哪个
用户"，重连凭证认的是"这是房间里的哪个玩家"。
"""

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.controller.dependencies import extract_bearer_token
from app.core.db import get_db
from app.core.errors import AppException, ErrorCode
from app.dto.auth import (
    AuthResult,
    ChangePasswordBody,
    LoginBody,
    MeRead,
    RegisterBody,
    UpdateNicknameBody,
)
from app.dto.common import ApiResponse
from app.service import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register", response_model=ApiResponse[AuthResult], status_code=status.HTTP_201_CREATED
)
async def register(
    payload: RegisterBody, db: AsyncSession = Depends(get_db)
) -> ApiResponse[AuthResult]:
    """POST /api/v1/auth/register —— 注册新账号，成功即登录。"""
    try:
        result = await auth_service.register(
            db, payload.account, payload.password, payload.nickname
        )
    except auth_service.AccountExistsError as exc:
        raise AppException(ErrorCode.CONFLICT, str(exc), status.HTTP_409_CONFLICT) from exc
    return ApiResponse.ok(result)


@router.post("/login", response_model=ApiResponse[AuthResult])
async def login(payload: LoginBody, db: AsyncSession = Depends(get_db)) -> ApiResponse[AuthResult]:
    """POST /api/v1/auth/login —— 账号密码登录。"""
    try:
        result = await auth_service.login(db, payload.account, payload.password)
    except auth_service.InvalidCredentialsError as exc:
        raise AppException(ErrorCode.UNAUTHORIZED, str(exc), status.HTTP_401_UNAUTHORIZED) from exc
    return ApiResponse.ok(result)


@router.post("/logout", response_model=ApiResponse[None])
async def logout(
    authorization: str | None = Header(default=None), db: AsyncSession = Depends(get_db)
) -> ApiResponse[None]:
    """POST /api/v1/auth/logout —— 退出登录，使当前 token 失效。"""
    await auth_service.logout(db, extract_bearer_token(authorization))
    return ApiResponse.ok(None)


@router.post("/password", response_model=ApiResponse[None])
async def change_password(
    payload: ChangePasswordBody,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[None]:
    """POST /api/v1/auth/password —— 改密码。

    要旧密码（token 可能来自一台没锁屏的机器）；成功后**这个账号的其它会话
    全部失效，当前这条保留**——改完当场把做对事的人踢回登录页没有道理。

    ⚠️ 这**不是**"找回密码"。忘了密码那条路要一个能收验证码的渠道，
    这个项目一样都没有（`exec/46` B6 留着那一半）。
    """
    try:
        await auth_service.change_password(
            db, extract_bearer_token(authorization), payload.old_password, payload.new_password
        )
    except (auth_service.AuthenticationError, auth_service.InvalidCredentialsError) as exc:
        # 两种都是 401：没带凭证 / 旧密码不对。**分开列不合并成 PermissionError**
        # ——那个基类以后可能被别的东西继承，而"什么该返回 401"是显式清单。
        raise AppException(ErrorCode.UNAUTHORIZED, str(exc), status.HTTP_401_UNAUTHORIZED) from exc
    except auth_service.SamePasswordError as exc:
        raise AppException(
            ErrorCode.VALIDATION_ERROR, str(exc), status.HTTP_400_BAD_REQUEST
        ) from exc
    return ApiResponse.ok(None)


@router.get("/me", response_model=ApiResponse[MeRead])
async def get_me(
    authorization: str | None = Header(default=None), db: AsyncSession = Depends(get_db)
) -> ApiResponse[MeRead]:
    """GET /api/v1/auth/me —— 获取当前登录用户，供刷新页面后恢复登录态使用。"""
    try:
        result = await auth_service.get_me(db, extract_bearer_token(authorization))
    except auth_service.AuthenticationError as exc:
        raise AppException(ErrorCode.UNAUTHORIZED, str(exc), status.HTTP_401_UNAUTHORIZED) from exc
    return ApiResponse.ok(result)


@router.patch("/me", response_model=ApiResponse[MeRead])
async def update_me(
    payload: UpdateNicknameBody,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[MeRead]:
    """PATCH /api/v1/auth/me —— 修改昵称（账号只读，本期不允许修改）。"""
    try:
        result = await auth_service.update_nickname(
            db, extract_bearer_token(authorization), payload.nickname
        )
    except auth_service.AuthenticationError as exc:
        raise AppException(ErrorCode.UNAUTHORIZED, str(exc), status.HTTP_401_UNAUTHORIZED) from exc
    return ApiResponse.ok(result)
