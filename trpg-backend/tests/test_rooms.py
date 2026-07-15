import pytest
from httpx import AsyncClient

from app.service import room as room_service

ROOMS_BASE = "/api/v1/rooms"


@pytest.fixture(autouse=True)
def clear_room_stub() -> None:
    room_service._rooms.clear()
    room_service._players.clear()


async def create_room(client: AsyncClient, max_players: int = 4) -> dict:
    response = await client.post(
        ROOMS_BASE,
        json={"roomName": "测试房间", "nickname": "房主", "maxPlayers": max_players},
    )
    assert response.status_code == 200
    return response.json()["data"]


def auth(token: str) -> dict[str, str]:
    return {"X-Reconnect-Token": token}


async def test_join_rejects_full_room(client: AsyncClient) -> None:
    room = await create_room(client, max_players=1)

    response = await client.post(f"{ROOMS_BASE}/{room['roomCode']}/join", json={"nickname": "玩家"})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"


async def test_join_rejects_room_after_story_starts(client: AsyncClient) -> None:
    room = await create_room(client)
    module_id = (await client.get("/api/v1/modules")).json()["data"][0]["id"]
    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/module",
        json={"moduleId": module_id, "attributeGenMethod": "point_buy"},
        headers=auth(room["reconnectToken"]),
    )
    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/start-story",
        json=None,
        headers=auth(room["reconnectToken"]),
    )

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomCode']}/join", json={"nickname": "迟到玩家"}
    )

    assert response.status_code == 409


async def test_host_operations_require_host_token(client: AsyncClient) -> None:
    room = await create_room(client)
    joined = (
        await client.post(f"{ROOMS_BASE}/{room['roomCode']}/join", json={"nickname": "玩家"})
    ).json()["data"]
    module_id = (await client.get("/api/v1/modules")).json()["data"][0]["id"]

    missing = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/module",
        json={"moduleId": module_id, "attributeGenMethod": "point_buy"},
    )
    non_host = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/module",
        json={"moduleId": module_id, "attributeGenMethod": "point_buy"},
        headers=auth(joined["reconnectToken"]),
    )

    assert missing.status_code == 401
    assert non_host.status_code == 403


async def test_select_module_validates_room_and_module(client: AsyncClient) -> None:
    room = await create_room(client)

    missing_room = await client.post(
        f"{ROOMS_BASE}/missing/module",
        json={"moduleId": "missing", "attributeGenMethod": "point_buy"},
        headers=auth(room["reconnectToken"]),
    )
    missing_module = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/module",
        json={"moduleId": "missing", "attributeGenMethod": "point_buy"},
        headers=auth(room["reconnectToken"]),
    )

    assert missing_room.status_code == 404
    assert missing_module.status_code == 404


async def test_list_my_rooms_filters_by_token(client: AsyncClient) -> None:
    first = await create_room(client)
    await create_room(client)

    response = await client.get("/api/v1/me/rooms", headers=auth(first["reconnectToken"]))

    assert response.status_code == 200
    rooms = response.json()["data"]
    assert [room["roomId"] for room in rooms] == [first["roomId"]]


async def test_room_text_fields_reject_whitespace(client: AsyncClient) -> None:
    response = await client.post(
        ROOMS_BASE,
        json={"roomName": "   ", "nickname": "房主", "maxPlayers": 4},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
