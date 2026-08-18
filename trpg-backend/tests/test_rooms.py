from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event
from app.models.room import Player, Room
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


async def test_a_latecomer_can_join_after_the_story_started(client: AsyncClient) -> None:
    """🔴 **行为变更**（2026-08-12）：这条原本断言中途加入被拒（`if room.phase !=
    "Lobby": raise`）。聚会的物理现实是有人晚到，而在此之前他连房间都进不来。

    仍然拒绝的只有已经结束的房间——见下一条。"""
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

    assert response.status_code == 200, response.text
    # 他还没建卡——这正是他进来之后要做的第一件事（`quick_build_character`
    # 填个名字就有一张完整的卡）。
    assert response.json()["data"]["characterId"] is None


async def test_join_is_still_refused_once_the_room_is_over(client: AsyncClient) -> None:
    """已经结束的房间没有"加入"可言——他要的是回放。"""
    room = await create_room(client)
    module_id = (await client.get("/api/v1/modules")).json()["data"][0]["id"]
    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/module",
        json={"moduleId": module_id, "attributeGenMethod": "point_buy"},
        headers=reconnect(room["reconnectToken"]),
    )
    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/disband",
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


async def test_unique_constraint_blocks_duplicate_player_rows(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """🔴 「一个账号在一个房间里只有一名玩家」必须由**数据库**保证（PR #110 review [2]）。

    service 层那段「先查有没有、没有才插」是 check-then-act：两个并发请求会同时
    查到「不存在」然后各插一行，幂等承诺当场失效、房间人数还会虚增。所以真正的
    不变式落在 `players` 的 `uq_players_room_user` 唯一约束上，service 只负责捕获
    `IntegrityError` 后把撞上的那一方收敛成和先到者一样的结果。

    这里直接往库里插重复行来证明**约束真的会咬人**——只测 service 的话，约束漏建
    了测试照样绿（service 那条 if 会先挡住），等真出现并发才暴露。
    并发下的收敛行为用真实 HTTP 并发验证（见 PR 说明），测试装置里的
    `ASGITransport` + aiosqlite 并发建连接会报 MissingGreenlet，测不了。
    """
    room = await create_room(client)
    guest_token = await register(client)
    joined = await join_room(client, room["roomCode"], guest_token)

    player = await db_session.scalar(select(Player).where(Player.id == joined["playerId"]))
    assert player is not None

    db_session.add(
        Player(
            room_id=player.room_id,
            user_id=player.user_id,
            nickname="影子玩家",
            is_host=False,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_join_returns_existing_character_id_for_reconnect(client: AsyncClient) -> None:
    """🔴 重连要能拿回自己那张角色卡的 id（PR #110 review [1]）。

    客户端靠 `characterId` 才知道去拉哪张卡。在此之前这个 id 只在建卡那一刻由
    客户端自己存着——**换台设备就永远拿不回来**，已经建完卡的人重连后会显示成
    「还没建卡」、被引导去建第二张。服务端本来就知道答案，直接给。
    """
    room = await create_room(client)
    guest_token = await register(client)
    joined = await join_room(client, room["roomCode"], guest_token)
    assert joined["characterId"] is None, "还没建卡时应该是 None"

    draft = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters",
        headers=reconnect(joined["reconnectToken"]),
    )
    character_id = draft.json()["data"]["characterId"]

    rejoined = await join_room(client, room["roomCode"], guest_token)

    assert rejoined["characterId"] == character_id


async def test_identity_says_whether_i_am_the_host(client: AsyncClient) -> None:
    """🔴 房主身份由服务端给，前端不许自己猜。

    在此之前前端是按**入口**猜的：建房那条路写死 true、加入那两条路写死 false。
    于是「换设备重进」（走的是加入那条路）的真房主拿到 false，开始游戏的按钮
    根本不显示——真机撞到过。
    """
    host_token = await register(client)
    room = await create_room(client, host_token)
    assert room["isHost"] is True

    guest_token = await register(client)
    guest = await join_room(client, room["roomCode"], guest_token)
    assert guest["isHost"] is False

    # 🔴 关键那一跑：房主换台设备重进，走的是跟访客同一条 join 路径。
    host_again = await join_room(client, room["roomCode"], host_token)
    assert host_again["playerId"] == room["playerId"]
    assert host_again["isHost"] is True


async def test_the_new_host_is_the_host_when_he_comes_back(client: AsyncClient) -> None:
    """转让房主之后，接手的人重进要认得出自己是房主。"""
    host_token = await register(client)
    room = await create_room(client, host_token)
    guest_token = await register(client)
    guest = await join_room(client, room["roomCode"], guest_token)

    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/host",
        json={"playerId": guest["playerId"]},
        headers=reconnect(room["reconnectToken"]),
    )

    assert (await join_room(client, room["roomCode"], guest_token))["isHost"] is True
    assert (await join_room(client, room["roomCode"], host_token))["isHost"] is False


async def test_list_my_rooms_query_count_does_not_grow_with_rooms(
    client: AsyncClient, sql_counter: list[str]
) -> None:
    """🔴 `/me/rooms` 的查询数必须跟房间数无关（PR #110 review [3]）。

    第一版在循环里逐个房间查人数、查模组标题，N 个房间约 `2N+2` 条查询。这个接口
    正是 #106 让它从「最多一个房间」变成「该账号全部房间」的，N 会真的长起来。

    用 SQLAlchemy 的 `before_cursor_execute` 数真实 SQL 条数：断言"总数有上限"而
    不是"等于某个具体值"，免得以后加一列无关的查询就误报。
    """
    token = await register(client, account="counter")
    module_id = (await client.get("/api/v1/modules")).json()["data"][0]["id"]
    for index in range(6):
        room = await create_room(client, token=token, room_name=f"房间{index}")
        # ⚠️ 必须真的选上模组：`_module_title` 在 `scenario_id` 为 NULL 时直接早退、
        # 一条查询都不发。第一版没选模组，于是"每个房间查一次标题"这条 N+1 路径
        # 根本没被触发——测试照样绿，是变异检验才发现的。
        await client.post(
            f"{ROOMS_BASE}/{room['roomId']}/module",
            json={"moduleId": module_id, "attributeGenMethod": "point_buy"},
            headers=reconnect(room["reconnectToken"]),
        )

    sql_counter.clear()  # 只数这一次请求的
    response = await client.get("/api/v1/me/rooms", headers=bearer(token))

    assert len(response.json()["data"]) == 6
    assert len(sql_counter) <= 8, (
        f"6 个房间发了 {len(sql_counter)} 条查询，说明查询数随房间数增长：\n"
        + "\n".join(sql_counter)
    )


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


async def _split_room_with_two_narrations(
    client: AsyncClient, db_session: AsyncSession
) -> tuple[dict, dict]:
    """造一间两人房，两段各自只发给一个人的分头叙事。返回 (房主, 队友)。"""
    host = await create_room(client, max_players=4)
    guest_account = await register(client)
    guest = await join_room(client, host["roomCode"], guest_account, nickname="阿贵")
    for owner, text in ((host, "地下室的味道很冲。"), (guest, "门廊上只有风声。")):
        db_session.add(
            Event(
                room_id=host["roomId"],
                player_id=None,
                event_type="narration.push",
                payload={"text": text, "audience": [owner["playerId"]]},
            )
        )
    await db_session.commit()
    return host, guest


async def test_replay_is_cut_by_audience_while_the_game_is_running(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """🔴 exec/33 §10 #78 的另一半：**刷新一次就把另一组的叙事全拿到了**。

    分头叙事落库时早就写了 `payload.audience`，但这个接口一直原样全返回——
    而前端进房/刷新正是靠它重建时间线。「加了字段没有消费方」。
    """
    host, guest = await _split_room_with_two_narrations(client, db_session)

    mine = await client.get(
        f"{ROOMS_BASE}/{host['roomId']}/replay", headers=reconnect(host["reconnectToken"])
    )
    assert [e["payload"]["text"] for e in mine.json()["data"]] == ["地下室的味道很冲。"]
    assert "门廊" not in mine.text

    theirs = await client.get(
        f"{ROOMS_BASE}/{host['roomId']}/replay", headers=reconnect(guest["reconnectToken"])
    )
    assert [e["payload"]["text"] for e in theirs.json()["data"]] == ["门廊上只有风声。"]
    assert "地下室" not in theirs.text


async def test_replay_opens_up_once_the_game_is_over(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """散场之后复盘全开：分头的保密前提是「你不在场」，而复盘的价值正是看见另一半。"""
    host, _guest = await _split_room_with_two_narrations(client, db_session)
    room = await db_session.get(Room, host["roomId"])
    assert room is not None
    room.phase = "Completed"
    await db_session.commit()

    response = await client.get(
        f"{ROOMS_BASE}/{host['roomId']}/replay", headers=reconnect(host["reconnectToken"])
    )
    assert len(response.json()["data"]) == 2


async def test_replay_never_leaks_the_keeper_decision_audit(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """🔴 exec/25 #61：裁决的审计事件**绝不能**出现在玩家的复盘里。

    `keeper.decision` 落 events 表是为了让我们能诊断"叙事为什么这么写"，而
    `get_replay` 是把 `payload` **原样**返回给玩家的：`thinking` 在 prompt 里
    就写明玩家看不到，`player_state` 会直接暴露守秘人对这句话的判断。

    造一条普通事件作对照——只断言"没有 keeper.decision"的话，把整个 replay
    改成返回空列表它也绿。
    """
    room = await create_room(client)
    room_id = room["roomId"]
    db_session.add(
        Event(
            room_id=room_id,
            player_id=room["playerId"],
            event_type="keeper.decision",
            payload={"player_state": "feasibility_question", "thinking": "玩家在问可行性"},
        )
    )
    db_session.add(
        Event(
            room_id=room_id,
            player_id=room["playerId"],
            event_type="narration.push",
            payload={"text": "门厅里空无一人。"},
        )
    )
    await db_session.commit()

    response = await client.get(
        f"{ROOMS_BASE}/{room_id}/replay", headers=reconnect(room["reconnectToken"])
    )
    assert response.status_code == 200
    events = response.json()["data"]
    # 对照：普通事件照常返回
    assert [e["eventType"] for e in events] == ["narration.push"]
    # 审计内容一个字都不能漏出去
    assert "玩家在问可行性" not in response.text
    assert "feasibility_question" not in response.text


async def test_replay_hides_every_audit_only_event(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """🔴 黑名单是**逐个列出**的，加一类审计事件就漏一类。

    2026-08-18 加 `keeper.progress`（每拍一条的无进展记账）时当场漏过一次：
    玩家的复盘时间线里会多出一串 `{"advanced": false}`。这条按**表**遍历，
    加一项时它跟着覆盖，不必再写一条新用例。

    🔴 **样本不许来自被测的那张表**：第一版拿 `_REPLAY_HIDDEN_EVENT_TYPES`
    自己当样本来源，于是从表里删掉一项之后循环也就不造那一项，变异体大摇大摆
    活下来——同 08-14 那条 `fields()` 遍历的守护测试瞎掉的形态。
    这里改成**独立列一份期望**，两头都钉：表变了这条会红，要么是真漏了，
    要么是有意加的、那就回来把这份期望改掉。

    **变异检验**：从 `_REPLAY_HIDDEN_EVENT_TYPES` 里去掉任意一项，这条当场红。
    """
    from app.service.room import _REPLAY_HIDDEN_EVENT_TYPES

    expected_hidden = {"keeper.decision", "keeper.progress"}
    assert expected_hidden == _REPLAY_HIDDEN_EVENT_TYPES, (
        "隐藏名单变了：确认新增的那一类确实是纯审计事件，然后更新这份期望"
    )

    room = await create_room(client)
    room_id = room["roomId"]
    for event_type in expected_hidden:
        db_session.add(
            Event(
                room_id=room_id,
                player_id=room["playerId"],
                event_type=event_type,
                payload={"audit_marker": f"{event_type}-不该出现"},
            )
        )
    # 对照：没有它，整个 replay 返回空列表也能绿
    db_session.add(
        Event(
            room_id=room_id,
            player_id=room["playerId"],
            event_type="narration.push",
            payload={"text": "门厅里空无一人。"},
        )
    )
    await db_session.commit()

    response = await client.get(
        f"{ROOMS_BASE}/{room_id}/replay", headers=reconnect(room["reconnectToken"])
    )
    assert [e["eventType"] for e in response.json()["data"]] == ["narration.push"]
    assert "不该出现" not in response.text
