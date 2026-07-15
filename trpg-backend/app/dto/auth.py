"""认证模块（issue #58）的 pydantic 请求/响应模型。

与 dto/room.py 的约定一致：JSON 层 camelCase，Python 层 snake_case。
"""

from typing import Annotated

from pydantic import AliasGenerator, BaseModel, ConfigDict, StringConstraints
from pydantic.alias_generators import to_camel


def _to_camel(snake: str) -> str:
    return to_camel(snake)


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=AliasGenerator(alias=_to_camel),
        populate_by_name=True,
    )


AccountStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=50)]
PasswordStr = Annotated[str, StringConstraints(min_length=6, max_length=100)]
NicknameStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class RegisterBody(CamelModel):
    """POST /api/v1/auth/register 请求体"""

    account: AccountStr
    password: PasswordStr
    nickname: NicknameStr


class LoginBody(CamelModel):
    """POST /api/v1/auth/login 请求体"""

    account: AccountStr
    password: PasswordStr


class UpdateNicknameBody(CamelModel):
    """PATCH /api/v1/auth/me 请求体"""

    nickname: NicknameStr


class AuthResult(CamelModel):
    """注册 / 登录成功后的返回：登录凭证 + 用户信息。"""

    token: str
    user_id: str
    nickname: str


class MeRead(CamelModel):
    """GET /PATCH /api/v1/auth/me 返回"""

    user_id: str
    account: str
    nickname: str
