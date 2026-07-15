"""Service 层：账号 + 会话（issue #58）—— MS1 内存 stub，跟 service/room.py 同款风格。

密码用 bcrypt 加盐哈希存储，不落明文；会话是随机 token，登录/注册时签发，
存在内存字典里换取用户，不做过期/续期（MS1 范围不包含"多设备会话管理"）。
"""

import asyncio
import hashlib
import uuid

import bcrypt

from app.dto.auth import AuthResult, MeRead

# ── 内存数据存储（MS1 stub，后续替换为数据库） ──
_users: dict[str, dict] = {}  # user_id -> {id, account, password_hash, nickname}
_accounts: dict[str, str] = {}  # account -> user_id
_tokens: dict[str, str] = {}  # token -> user_id


class AccountExistsError(ValueError):
    """账号已被注册。"""


class InvalidCredentialsError(PermissionError):
    """账号或密码不正确。"""


class AuthenticationError(PermissionError):
    """未提供有效的登录凭证。"""


def _prehash(password: str) -> bytes:
    """bcrypt 只认密码的前 72 字节，超出部分会被静默截断——密码开到 100 字符
    时，两个仅在 72 字节之后不同的密码会算出同一个哈希，等于变相碰撞（code
    review 用真实 bcrypt 4.3.0 复现过）。这里先用 sha256 摘要成固定 32 字节
    再喂给 bcrypt，从根源上避开这个截断限制，而不是缩短密码长度上限。
    """
    return hashlib.sha256(password.encode()).digest()


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode()


def _verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(_prehash(password), password_hash.encode())


async def register(account: str, password: str, nickname: str) -> AuthResult:
    """注册新账号，成功即视为登录，直接签发 token。"""
    if account in _accounts:
        raise AccountExistsError("账号已被注册")
    user_id = str(uuid.uuid4())
    # bcrypt 是同步、CPU 密集的调用，直接在协程里跑会阻塞事件循环处理其它
    # 并发请求，丢进线程池执行。
    password_hash = await asyncio.to_thread(_hash_password, password)
    _users[user_id] = {
        "id": user_id,
        "account": account,
        "password_hash": password_hash,
        "nickname": nickname,
    }
    _accounts[account] = user_id
    token = str(uuid.uuid4())
    _tokens[token] = user_id
    return AuthResult(token=token, user_id=user_id, nickname=nickname)


async def login(account: str, password: str) -> AuthResult:
    """账号密码登录。"""
    user_id = _accounts.get(account)
    user = _users.get(user_id) if user_id else None
    if user is None:
        raise InvalidCredentialsError("账号或密码不正确")
    verified = await asyncio.to_thread(_verify_password, password, user["password_hash"])
    if not verified:
        raise InvalidCredentialsError("账号或密码不正确")
    token = str(uuid.uuid4())
    _tokens[token] = user["id"]
    return AuthResult(token=token, user_id=user["id"], nickname=user["nickname"])


async def logout(token: str | None) -> None:
    """退出登录，使当前 token 失效。"""
    if token is not None:
        _tokens.pop(token, None)


def _get_user_by_token(token: str | None) -> dict:
    if token is None:
        raise AuthenticationError("缺少登录凭证")
    user_id = _tokens.get(token)
    user = _users.get(user_id) if user_id else None
    if user is None:
        raise AuthenticationError("登录凭证无效")
    return user


async def get_me(token: str | None) -> MeRead:
    """获取当前登录用户。"""
    user = _get_user_by_token(token)
    return MeRead(user_id=user["id"], account=user["account"], nickname=user["nickname"])


async def update_nickname(token: str | None, nickname: str) -> MeRead:
    """修改当前登录用户的昵称（账号只读，本期不允许修改）。"""
    user = _get_user_by_token(token)
    user["nickname"] = nickname
    return MeRead(user_id=user["id"], account=user["account"], nickname=user["nickname"])
