from httpx import AsyncClient

AUTH_BASE = "/api/v1/auth"


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


async def test_long_passwords_differing_after_bcrypt_limit_are_not_confused(
    client: AsyncClient,
) -> None:
    # bcrypt 本身只认密码的前 72 字节，两个仅在这之后不同的密码如果直接喂给
    # bcrypt 会被当成同一个密码——这里用两个前 72 字节相同、只有末尾不同的
    # 100 字符密码验证不会互相通过登录（回归 service/auth.py 里的 sha256
    # 预哈希修复）。
    password_a = "a" * 99 + "A"
    password_b = "a" * 99 + "B"
    await client.post(
        f"{AUTH_BASE}/register",
        json={"account": "carol", "password": password_a, "nickname": "卡罗尔"},
    )

    wrong = await client.post(
        f"{AUTH_BASE}/login", json={"account": "carol", "password": password_b}
    )
    correct = await client.post(
        f"{AUTH_BASE}/login", json={"account": "carol", "password": password_a}
    )

    assert wrong.status_code == 401
    assert correct.status_code == 200


# ── 改密码（`exec/46` B6）─────────────────────────────


async def test_password_change_takes_effect(client: AsyncClient) -> None:
    """改完之后：新密码登得进，旧密码登不进。**两头都要验**。

    只验"新密码能登"的话，一个什么都不做的实现照样绿（旧密码本来就能登）。
    """
    data = await register(client)
    response = await client.post(
        f"{AUTH_BASE}/password",
        json={"oldPassword": "secret1", "newPassword": "secret2"},
        headers=bearer(data["token"]),
    )
    assert response.status_code == 200

    ok = await client.post(f"{AUTH_BASE}/login", json={"account": "alice", "password": "secret2"})
    assert ok.status_code == 200
    stale = await client.post(
        f"{AUTH_BASE}/login", json={"account": "alice", "password": "secret1"}
    )
    assert stale.status_code == 401


async def test_password_change_needs_the_old_one(client: AsyncClient) -> None:
    """🔴 光凭 token 就能改的话，一台没锁屏的机器 = 账号永久易主
    ——那正是改密码本该解决的场景。"""
    data = await register(client)
    response = await client.post(
        f"{AUTH_BASE}/password",
        json={"oldPassword": "wrong-one", "newPassword": "secret2"},
        headers=bearer(data["token"]),
    )
    assert response.status_code == 401
    # 密码没被改动
    ok = await client.post(f"{AUTH_BASE}/login", json={"account": "alice", "password": "secret1"})
    assert ok.status_code == 200


async def test_password_change_requires_login(client: AsyncClient) -> None:
    response = await client.post(
        f"{AUTH_BASE}/password", json={"oldPassword": "secret1", "newPassword": "secret2"}
    )
    assert response.status_code == 401


async def test_the_same_password_is_refused_as_a_bad_request(client: AsyncClient) -> None:
    """跟原密码一样是"填的内容不合要求"（400），不是"凭证不对"（401）——
    两者对用户意味着完全不同的下一步。"""
    data = await register(client)
    response = await client.post(
        f"{AUTH_BASE}/password",
        json={"oldPassword": "secret1", "newPassword": "secret1"},
        headers=bearer(data["token"]),
    )
    assert response.status_code == 400


async def test_other_sessions_are_kicked_but_this_one_survives(client: AsyncClient) -> None:
    """🔴 **改密码最常见的理由是"我怀疑别人登进去了"。**

    只改哈希不动会话的话，那个人手上的 token 照样有效——改了等于没改。
    而当前这条要留着：改完当场把做对事的人踢回登录页没有道理。
    """
    first = await register(client)
    second = await client.post(
        f"{AUTH_BASE}/login", json={"account": "alice", "password": "secret1"}
    )
    other_token = second.json()["data"]["token"]
    # 装置自证：两条会话此刻都是活的
    assert (await client.get(f"{AUTH_BASE}/me", headers=bearer(first["token"]))).status_code == 200
    assert (await client.get(f"{AUTH_BASE}/me", headers=bearer(other_token))).status_code == 200

    await client.post(
        f"{AUTH_BASE}/password",
        json={"oldPassword": "secret1", "newPassword": "secret2"},
        headers=bearer(first["token"]),
    )

    mine = await client.get(f"{AUTH_BASE}/me", headers=bearer(first["token"]))
    theirs = await client.get(f"{AUTH_BASE}/me", headers=bearer(other_token))
    assert mine.status_code == 200, "改密码的人自己被踢下线了"
    assert theirs.status_code == 401, "别的会话还活着 —— 改密码等于没改"
