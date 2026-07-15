import pytest
from httpx import AsyncClient

from app.service import auth as auth_service

AUTH_BASE = "/api/v1/auth"


@pytest.fixture(autouse=True)
def clear_auth_stub() -> None:
    auth_service._users.clear()
    auth_service._accounts.clear()
    auth_service._tokens.clear()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def register(client: AsyncClient, account: str = "alice", password: str = "secret1") -> dict:
    response = await client.post(
        f"{AUTH_BASE}/register",
        json={"account": account, "password": password, "nickname": "爱丽丝"},
    )
    assert response.status_code == 201
    return response.json()["data"]


async def test_register_then_login_succeeds(client: AsyncClient) -> None:
    await register(client)

    response = await client.post(
        f"{AUTH_BASE}/login", json={"account": "alice", "password": "secret1"}
    )

    assert response.status_code == 200
    assert response.json()["data"]["nickname"] == "爱丽丝"


async def test_register_rejects_duplicate_account(client: AsyncClient) -> None:
    await register(client)

    response = await client.post(
        f"{AUTH_BASE}/register",
        json={"account": "alice", "password": "secret2", "nickname": "另一个"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


async def test_login_rejects_wrong_password(client: AsyncClient) -> None:
    await register(client)

    response = await client.post(
        f"{AUTH_BASE}/login", json={"account": "alice", "password": "wrong-password"}
    )

    assert response.status_code == 401


async def test_me_requires_valid_token(client: AsyncClient) -> None:
    missing = await client.get(f"{AUTH_BASE}/me")
    invalid = await client.get(f"{AUTH_BASE}/me", headers=bearer("not-a-real-token"))

    assert missing.status_code == 401
    assert invalid.status_code == 401


async def test_me_reflects_session_after_login(client: AsyncClient) -> None:
    session = await register(client)

    response = await client.get(f"{AUTH_BASE}/me", headers=bearer(session["token"]))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["account"] == "alice"
    assert data["nickname"] == "爱丽丝"


async def test_update_nickname_persists(client: AsyncClient) -> None:
    session = await register(client)

    updated = await client.patch(
        f"{AUTH_BASE}/me", json={"nickname": "新昵称"}, headers=bearer(session["token"])
    )
    me = await client.get(f"{AUTH_BASE}/me", headers=bearer(session["token"]))

    assert updated.status_code == 200
    assert updated.json()["data"]["nickname"] == "新昵称"
    assert me.json()["data"]["nickname"] == "新昵称"


async def test_logout_invalidates_token(client: AsyncClient) -> None:
    session = await register(client)

    logout_response = await client.post(f"{AUTH_BASE}/logout", headers=bearer(session["token"]))
    me_after_logout = await client.get(f"{AUTH_BASE}/me", headers=bearer(session["token"]))

    assert logout_response.status_code == 200
    assert me_after_logout.status_code == 401


async def test_register_rejects_short_password(client: AsyncClient) -> None:
    response = await client.post(
        f"{AUTH_BASE}/register",
        json={"account": "bob", "password": "123", "nickname": "鲍勃"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
