"""房间成员管理四件套：踢人 / 转让房主 / 改人数上限 / 解散。

聚会场景的现实：人来错了房间、房主要提前走、位置不够、人没凑齐。这四条此前
一条都没有——房间建出来之后成员就是只增不减的。

🔴 **踢人只在大厅**，理由写在 `service/room.py` 那一段：对局中踢人要连带处理
位置、待掷队列、分组、会合确认，而"开局之后想把人赶走"是社交问题。
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.room import Room
from tests.helpers import ROOMS_BASE, bearer, create_room, join_room, reconnect, register


async def _room_with_two(client: AsyncClient) -> tuple[dict, dict]:
    """房主 + 一名玩家。"""
    room = await create_room(client)
    guest_token = await register(client)
    guest = await join_room(client, room["roomCode"], guest_token, nickname="阿福")
    return room, guest


async def _players(client: AsyncClient, room_code: str) -> list[dict]:
    response = await client.get(f"{ROOMS_BASE}/{room_code}")
    assert response.status_code == 200, response.text
    return response.json()["data"]["players"]


# ── 踢人 ─────────────────────────────────────────


async def test_the_host_can_remove_someone_in_the_lobby(client: AsyncClient) -> None:
    room, guest = await _room_with_two(client)

    response = await client.delete(
        f"{ROOMS_BASE}/{room['roomId']}/players/{guest['playerId']}",
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 200, response.text
    assert [p["playerId"] for p in await _players(client, room["roomCode"])] == [room["playerId"]]


async def test_a_kicked_seat_is_really_free(client: AsyncClient) -> None:
    """🔴 光看列表少一行不够：踢掉之后那个位置必须真的能再坐人。
    只把 `Player` 标记成"已移出"的实现能通过上一条，过不了这条。"""
    room = await create_room(client, max_players=2)
    guest_token = await register(client)
    guest = await join_room(client, room["roomCode"], guest_token, nickname="阿福")

    await client.delete(
        f"{ROOMS_BASE}/{room['roomId']}/players/{guest['playerId']}",
        headers=reconnect(room["reconnectToken"]),
    )

    another = await register(client)
    response = await client.post(
        f"{ROOMS_BASE}/{room['roomCode']}/join",
        json={"nickname": "阿贵"},
        headers=bearer(another),
    )
    assert response.status_code == 200, response.text


async def test_only_the_host_can_kick(client: AsyncClient) -> None:
    room, guest = await _room_with_two(client)

    response = await client.delete(
        f"{ROOMS_BASE}/{room['roomId']}/players/{room['playerId']}",
        headers=reconnect(guest["reconnectToken"]),
    )

    assert response.status_code == 403


async def test_the_host_cannot_kick_himself(client: AsyncClient) -> None:
    """否则房间会剩下一屋子没有房主的人：选模组、开局、解散从此全做不了。"""
    room, _guest = await _room_with_two(client)

    response = await client.delete(
        f"{ROOMS_BASE}/{room['roomId']}/players/{room['playerId']}",
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 409
    assert "转让" in response.json()["error"]["message"]


async def test_kicking_is_refused_once_the_story_started(client: AsyncClient) -> None:
    room, guest = await _room_with_two(client)
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

    response = await client.delete(
        f"{ROOMS_BASE}/{room['roomId']}/players/{guest['playerId']}",
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 409


async def test_a_player_from_another_room_cannot_be_kicked(client: AsyncClient) -> None:
    """`playerId` 是客户端给的——跨房间操作必须当场拒绝。"""
    room, _guest = await _room_with_two(client)
    other_room, other_guest = await _room_with_two(client)

    response = await client.delete(
        f"{ROOMS_BASE}/{room['roomId']}/players/{other_guest['playerId']}",
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 404
    assert [p["playerId"] for p in await _players(client, other_room["roomCode"])] == [
        other_room["playerId"],
        other_guest["playerId"],
    ]


# ── 转让房主 ─────────────────────────────────────


async def test_transferring_the_host_moves_both_markers(client: AsyncClient) -> None:
    """🔴 `is_host`（前端看的）与 `host_player_id`（后端认的）是两处，
    只改一处就会出现"显示他是房主，但他做什么都 403"。"""
    room, guest = await _room_with_two(client)

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/host",
        json={"playerId": guest["playerId"]},
        headers=reconnect(room["reconnectToken"]),
    )
    assert response.status_code == 200, response.text

    hosts = {p["playerId"]: p["isHost"] for p in await _players(client, room["roomCode"])}
    assert hosts[guest["playerId"]] is True
    assert hosts[room["playerId"]] is False

    # 后端认的那一处：新房主现在能做房主的事，老房主不能
    module_id = (await client.get("/api/v1/modules")).json()["data"][0]["id"]
    ok = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/module",
        json={"moduleId": module_id, "attributeGenMethod": "point_buy"},
        headers=reconnect(guest["reconnectToken"]),
    )
    assert ok.status_code == 200, ok.text
    refused = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/module",
        json={"moduleId": module_id, "attributeGenMethod": "point_buy"},
        headers=reconnect(room["reconnectToken"]),
    )
    assert refused.status_code == 403


async def test_the_host_cannot_be_handed_to_an_ai(client: AsyncClient) -> None:
    """AI 拿不到 reconnect_token、也永远不会去点开始游戏——给它等于房间没有房主。"""
    room = await create_room(client)
    module_id = (await client.get("/api/v1/modules")).json()["data"][0]["id"]
    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/module",
        json={"moduleId": module_id, "attributeGenMethod": "point_buy"},
        headers=reconnect(room["reconnectToken"]),
    )
    created = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/ai-players",
        json={"nickname": "阿铁"},
        headers=reconnect(room["reconnectToken"]),
    )
    assert created.status_code == 201, created.text

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/host",
        json={"playerId": created.json()["data"]["playerId"]},
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 409


async def test_transferring_the_host_works_after_the_story_started(client: AsyncClient) -> None:
    """不限阶段：真实场景恰恰是**开局之后**房主要先走。"""
    room, guest = await _room_with_two(client)
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
        f"{ROOMS_BASE}/{room['roomId']}/host",
        json={"playerId": guest["playerId"]},
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 200, response.text


# ── 人数上限 ─────────────────────────────────────


async def test_raising_the_cap_lets_one_more_person_in(client: AsyncClient) -> None:
    room = await create_room(client, max_players=1)
    latecomer = await register(client)
    full = await client.post(
        f"{ROOMS_BASE}/{room['roomCode']}/join",
        json={"nickname": "阿福"},
        headers=bearer(latecomer),
    )
    assert full.status_code == 409

    bumped = await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}",
        json={"maxPlayers": 4},
        headers=reconnect(room["reconnectToken"]),
    )
    assert bumped.status_code == 200, bumped.text

    retry = await client.post(
        f"{ROOMS_BASE}/{room['roomCode']}/join",
        json={"nickname": "阿福"},
        headers=bearer(latecomer),
    )
    assert retry.status_code == 200, retry.text


async def test_the_cap_cannot_drop_below_the_people_already_seated(client: AsyncClient) -> None:
    """调到比在座的人还少 = 让已经在玩的人凭空超员，而没有任何代码会去踢掉多出来的。"""
    room, _guest = await _room_with_two(client)

    response = await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}",
        json={"maxPlayers": 1},
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 409


async def test_only_the_host_changes_the_cap(client: AsyncClient) -> None:
    room, guest = await _room_with_two(client)

    response = await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}",
        json={"maxPlayers": 8},
        headers=reconnect(guest["reconnectToken"]),
    )

    assert response.status_code == 403


# ── 解散 ─────────────────────────────────────────


async def test_disbanding_ends_the_room_but_keeps_the_replay(client: AsyncClient) -> None:
    """🔴 解散不删数据：一屋子人刚玩过，回放不该对他们变成 404。"""
    room, guest = await _room_with_two(client)

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/disband",
        json=None,
        headers=reconnect(room["reconnectToken"]),
    )
    assert response.status_code == 200, response.text

    preview = await client.get(f"{ROOMS_BASE}/{room['roomCode']}")
    assert preview.json()["data"]["phase"] == "Completed"
    replay = await client.get(
        f"{ROOMS_BASE}/{room['roomId']}/replay",
        headers=reconnect(guest["reconnectToken"]),
    )
    assert replay.status_code == 200, replay.text


async def test_disbanding_works_in_the_lobby_where_end_does_not(client: AsyncClient) -> None:
    """两条接口的分工：`end` 是"把这局收掉"（要 InGame），`disband` 是"散了"。"""
    room, _guest = await _room_with_two(client)

    ended = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/end",
        json=None,
        headers=reconnect(room["reconnectToken"]),
    )
    assert ended.status_code == 409

    disbanded = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/disband",
        json=None,
        headers=reconnect(room["reconnectToken"]),
    )
    assert disbanded.status_code == 200, disbanded.text


async def test_disbanding_twice_is_refused(client: AsyncClient) -> None:
    room, _guest = await _room_with_two(client)
    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/disband",
        json=None,
        headers=reconnect(room["reconnectToken"]),
    )

    again = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/disband",
        json=None,
        headers=reconnect(room["reconnectToken"]),
    )

    assert again.status_code == 409


async def test_only_the_host_disbands(client: AsyncClient) -> None:
    room, guest = await _room_with_two(client)

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/disband",
        json=None,
        headers=reconnect(guest["reconnectToken"]),
    )

    assert response.status_code == 403


# ── 中途离开 / 回来 ──────────────────────────────
#
# ⚠️ 这几条要读 `keeper_state`，走 `db_session` fixture 而不是自己 import
# conftest 的 session 工厂——后者会把 conftest 当成另一个模块再导入一次、
# 连带新建一个引擎，表建在旧引擎上，结果是 `no such table`（conftest 里
# 那段注释专门写了这条）。


async def _keeper_state(db_session: AsyncSession, room: dict) -> dict:
    row = await db_session.get(Room, room["roomId"])
    assert row is not None
    await db_session.refresh(row)
    return dict(row.keeper_state or {})


async def test_a_player_can_step_away_and_come_back(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    room, guest = await _room_with_two(client)

    away = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/players/{guest['playerId']}/away",
        json={"away": True},
        headers=reconnect(guest["reconnectToken"]),
    )
    assert away.status_code == 200, away.text
    assert guest["playerId"] in (await _keeper_state(db_session, room))["待交代离场"]

    back = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/players/{guest['playerId']}/away",
        json={"away": False},
        headers=reconnect(guest["reconnectToken"]),
    )
    assert back.status_code == 200, back.text
    assert guest["playerId"] not in (await _keeper_state(db_session, room))["待交代离场"]


async def test_coming_back_puts_him_back_in_the_line_to_be_introduced(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """🔴 「已交代登场」是累积集合。回来时不把他摘出去的话，他从故事里消失
    又出现，而守秘人一个字都不会提。第一版实现漏了这一步。"""
    room, guest = await _room_with_two(client)
    row = await db_session.get(Room, room["roomId"])
    assert row is not None
    row.keeper_state = {"已交代登场": f"{room['playerId']}, {guest['playerId']}"}
    await db_session.commit()

    for value in (True, False):
        response = await client.post(
            f"{ROOMS_BASE}/{room['roomId']}/players/{guest['playerId']}/away",
            json={"away": value},
            headers=reconnect(guest["reconnectToken"]),
        )
        assert response.status_code == 200, response.text

    state = await _keeper_state(db_session, room)
    assert guest["playerId"] not in state["已交代登场"]
    assert room["playerId"] in state["已交代登场"]


async def test_the_host_can_mark_someone_else_away(client: AsyncClient) -> None:
    """他人已经走了、手机还揣兜里——那时得有人替他按。"""
    room, guest = await _room_with_two(client)

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/players/{guest['playerId']}/away",
        json={"away": True},
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 200, response.text


async def test_a_bystander_cannot_send_someone_else_away(client: AsyncClient) -> None:
    room, guest = await _room_with_two(client)
    third_token = await register(client)
    third = await join_room(client, room["roomCode"], third_token, nickname="阿贵")

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/players/{guest['playerId']}/away",
        json={"away": True},
        headers=reconnect(third["reconnectToken"]),
    )

    assert response.status_code == 403
