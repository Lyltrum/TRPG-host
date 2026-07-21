from datetime import datetime

from httpx import AsyncClient

from tests.helpers import ROOMS_BASE, bearer, create_room, join_room, reconnect, register


async def test_join_rejects_full_room(client: AsyncClient) -> None:
    room = await create_room(client, max_players=1)
    latecomer = await register(client)

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomCode']}/join",
        json={"nickname": "玩家"},
        headers=bearer(latecomer),
    )

    assert response.status_code == 409
    # 满房间返回更具体的 ROOM_FULL（issue #77 新增的业务错误码），不再是泛化的 CONFLICT。
    assert response.json()["error"]["code"] == "ROOM_FULL"


async def test_join_rejects_new_player_after_story_starts(client: AsyncClient) -> None:
    """中途加入仍然拒绝——但这条只针对**新人**，老成员重连见下面那条对照用例。"""
    room = await create_room(client)
    module_id = (await client.get("/api/v1/modules")).json()["data"][0]["id"]
    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/module",
        json={"moduleId": module_id, "attributeGenMethod": "point_buy"},
        headers=reconnect(room["reconnectToken"]),
    )
    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/start-story",
        json=None,
        headers=reconnect(room["reconnectToken"]),
    )
    latecomer = await register(client)

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomCode']}/join",
        json={"nickname": "迟到玩家"},
        headers=bearer(latecomer),
    )

    assert response.status_code == 409


async def test_rejoin_is_idempotent_and_returns_the_same_identity(client: AsyncClient) -> None:
    """🔴 同一个账号重复加入 = 重连，必须拿回**同一个** playerId，且不新增玩家行。

    改动前 `join_room` 全程不检查调用者是不是已经在房间里，无条件新建 `Player`
    ——同一个人重复加入会生成重复玩家行、虚增人数直到撞满员。
    """
    room = await create_room(client, max_players=4)
    guest_token = await register(client)
    first = await join_room(client, room["roomCode"], guest_token)

    second = await join_room(client, room["roomCode"], guest_token)

    assert second["playerId"] == first["playerId"]
    assert second["reconnectToken"] == first["reconnectToken"]

    preview = await client.get(f"{ROOMS_BASE}/{room['roomCode']}")
    assert len(preview.json()["data"]["players"]) == 2, "重复加入不该多出一个玩家"


async def test_existing_member_can_rejoin_after_story_starts(client: AsyncClient) -> None:
    """🔴 老成员在 Building 阶段必须能重连回来——这是 #106 要修的核心缺陷。

    改动前 `join_room` 开头一律 `if room.phase != "Lobby": raise`，把「中途加入」
    和「掉线重连」当成同一件事拒掉，结果刷新/断线后必现 409、回不到自己那局。
    跟上面 `test_join_rejects_new_player_after_story_starts` 是一对对照：新人要拒、
    老成员要放行，只测一边的话，"一刀切拒绝"和"一刀切放行"各能通过一条。
    """
    room = await create_room(client)
    guest_token = await register(client)
    joined = await join_room(client, room["roomCode"], guest_token)

    module_id = (await client.get("/api/v1/modules")).json()["data"][0]["id"]
    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/module",
        json={"moduleId": module_id, "attributeGenMethod": "point_buy"},
        headers=reconnect(room["reconnectToken"]),
    )
    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/start-story",
        json=None,
        headers=reconnect(room["reconnectToken"]),
    )

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomCode']}/join",
        json={"nickname": "访客"},
        headers=bearer(guest_token),
    )

    assert response.status_code == 200, "老成员重连不该被阶段拦住"
    assert response.json()["data"]["playerId"] == joined["playerId"]


async def test_rejoin_is_not_blocked_by_a_full_room(client: AsyncClient) -> None:
    """满员不该拦住老成员重连——他本来就已经占着那个位置。"""
    room = await create_room(client, max_players=2)
    guest_token = await register(client)
    joined = await join_room(client, room["roomCode"], guest_token)

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomCode']}/join",
        json={"nickname": "玩家"},
        headers=bearer(guest_token),
    )

    assert response.status_code == 200
    assert response.json()["data"]["playerId"] == joined["playerId"]


async def test_created_room_is_linked_to_the_account(client: AsyncClient) -> None:
    """建房要把房间和房主玩家都关联到真实账号，不能留空。"""
    token = await register(client, account="host")
    room = await create_room(client, token=token)

    rooms = (await client.get("/api/v1/me/rooms", headers=bearer(token))).json()["data"]

    assert [r["roomId"] for r in rooms] == [room["roomId"]], (
        "房间没跟账号关联上，「我的游戏」就查不到它"
    )


async def test_host_operations_require_host_token(client: AsyncClient) -> None:
    room = await create_room(client)
    guest_token = await register(client)
    joined = await join_room(client, room["roomCode"], guest_token)
    module_id = (await client.get("/api/v1/modules")).json()["data"][0]["id"]

    missing = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/module",
        json={"moduleId": module_id, "attributeGenMethod": "point_buy"},
    )
    non_host = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/module",
        json={"moduleId": module_id, "attributeGenMethod": "point_buy"},
        headers=reconnect(joined["reconnectToken"]),
    )

    assert missing.status_code == 401
    assert non_host.status_code == 403


async def test_select_module_validates_room_and_module(client: AsyncClient) -> None:
    room = await create_room(client)

    missing_room = await client.post(
        f"{ROOMS_BASE}/missing/module",
        json={"moduleId": "missing", "attributeGenMethod": "point_buy"},
        headers=reconnect(room["reconnectToken"]),
    )
    missing_module = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/module",
        json={"moduleId": "missing", "attributeGenMethod": "point_buy"},
        headers=reconnect(room["reconnectToken"]),
    )

    assert missing_room.status_code == 404
    assert missing_module.status_code == 404


async def test_create_and_join_require_login(client: AsyncClient) -> None:
    """issue #106：创建和加入房间都要求登录。

    改动前它们用的是"登录可选"的依赖，于是 `host_user_id`/`user_id` 永远是空的
    ——一个可以不填的身份字段，在没人强制时就永远不会被填上。
    """
    no_auth_create = await client.post(
        ROOMS_BASE, json={"roomName": "房间", "nickname": "房主", "maxPlayers": 4}
    )
    assert no_auth_create.status_code == 401

    room = await create_room(client)
    no_auth_join = await client.post(
        f"{ROOMS_BASE}/{room['roomCode']}/join", json={"nickname": "玩家"}
    )
    assert no_auth_join.status_code == 401

    bad_token_join = await client.post(
        f"{ROOMS_BASE}/{room['roomCode']}/join",
        json={"nickname": "玩家"},
        headers=bearer("not-a-real-token"),
    )
    assert bad_token_join.status_code == 401


async def test_list_my_rooms_returns_all_rooms_of_the_account(client: AsyncClient) -> None:
    """「我的游戏」按账号返回**全部**房间（issue #106）。

    改动前是按 `reconnect_token` 查的，而一个凭证只对应一名玩家/一个房间——
    「我的游戏」实际上是「这个浏览器的最后一个房间」，换台设备就什么都看不到。
    """
    token = await register(client, account="owner")
    first = await create_room(client, token=token, room_name="第一间")
    second = await create_room(client, token=token, room_name="第二间")

    response = await client.get("/api/v1/me/rooms", headers=bearer(token))

    assert response.status_code == 200
    room_ids = {room["roomId"] for room in response.json()["data"]}
    assert room_ids == {first["roomId"], second["roomId"]}


async def test_list_my_rooms_does_not_leak_other_accounts(client: AsyncClient) -> None:
    """上一条的对照面：只按账号过滤，别人的房间不能出现在我的列表里。

    只测「返回了我的两个房间」的话，一个"返回全部房间"的错误实现照样能通过。
    """
    mine_token = await register(client, account="mine")
    mine = await create_room(client, token=mine_token)
    other_token = await register(client, account="other")
    await create_room(client, token=other_token)

    response = await client.get("/api/v1/me/rooms", headers=bearer(mine_token))

    assert [room["roomId"] for room in response.json()["data"]] == [mine["roomId"]]


async def test_timestamps_are_returned_with_timezone(client: AsyncClient) -> None:
    """🔴 对外暴露的时间必须带时区后缀。

    数据库里写的是 `datetime.now(UTC)`，但 **SQLite 不保存时区信息**，读出来是
    naive datetime，序列化就成了 `2026-07-21T09:35:13.095627`——没有任何后缀。
    客户端按 ECMAScript 规则会把它当**本地时间**解析，于是「4 分钟前」在 UTC+8
    的机器上显示成「8 小时前」，偏差正好是时区偏移量（真机验证时实测到的）。

    这不是客户端该自己猜的事：光看字符串无从判断它是 UTC 还是本地时间。
    """
    token = await register(client)
    await create_room(client, token=token)

    rooms = (await client.get("/api/v1/me/rooms", headers=bearer(token))).json()["data"]

    updated_at = rooms[0]["updatedAt"]
    assert updated_at.endswith("Z") or "+" in updated_at, (
        f"时间戳缺时区后缀：{updated_at}——客户端会把它当本地时间解析"
    )
    assert datetime.fromisoformat(updated_at).tzinfo is not None


async def test_list_my_rooms_requires_login(client: AsyncClient) -> None:
    response = await client.get("/api/v1/me/rooms")

    assert response.status_code == 401


async def test_room_text_fields_reject_whitespace(client: AsyncClient) -> None:
    token = await register(client)
    response = await client.post(
        ROOMS_BASE,
        json={"roomName": "   ", "nickname": "房主", "maxPlayers": 4},
        headers=bearer(token),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_replay_requires_room_member_token(client: AsyncClient) -> None:
    """回放是"只有参与者能看"的内容——没有有效的房间成员凭证不能拉（PR #78 review）。"""
    room = await create_room(client)
    room_id = room["roomId"]

    # 无凭证 → 401
    no_token = await client.get(f"{ROOMS_BASE}/{room_id}/replay")
    assert no_token.status_code == 401

    # 别的房间的成员凭证 → 403（凭证有效但不是这个房间的成员）
    other = await create_room(client)
    wrong_room = await client.get(
        f"{ROOMS_BASE}/{room_id}/replay", headers=reconnect(other["reconnectToken"])
    )
    assert wrong_room.status_code == 403

    # 本房间成员凭证 → 200
    ok = await client.get(
        f"{ROOMS_BASE}/{room_id}/replay", headers=reconnect(room["reconnectToken"])
    )
    assert ok.status_code == 200
    assert ok.json()["data"] == []
