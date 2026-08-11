"""issue #107 的 WS/REST 行为测试：讨论区落库与回显、行动锁、历史分页、退房清理。

复用 tests/test_ws.py 的装置模式（同步 TestClient + websocket_connect）。

⚠️ 本文件全部用**单条 WS 连接**：Starlette TestClient 的每个 websocket_connect
各起一个独立 portal 线程 + 独立事件循环，同一房间开两条连接时，广播要往另一个
事件循环的 websocket send —— 跨循环 await 直接挂死（conftest.py 顶部注释里
"各循环各连接"说的就是这件事；test_ws.py 现有的双连接用例之所以能跑，是因为
那两条连接在**不同房间**、从不互相广播）。「同房间双客户端都能收到广播」这类
断言只有 SDK e2e（真 uvicorn、单事件循环）能做——见
e2e/tests/discussion-chat.e2e.ts，那边有完整的双客户端覆盖；这里守住的是
落库/幂等/锁语义/鉴权/清理这些单连接就能证明的行为。

锁相关用例通过覆盖 `app.state.narrator` 注入可控的 fake——narrator 挂在
app.state 上（而不是模块内部单例）正是为了让测试不需要 monkeypatch。
"""

from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from app.core.narration.contract import (
    CheckResultCallback,
    CheckResultNotice,
    NarrationContext,
    NarrationOutcome,
    Narrator,
)
from app.core.narration.fallback import FallbackNarrator
from app.main import app
from app.service.action_lock import RoomActionLockManager
from tests.helpers import next_game_event
from tests.test_ws import ROOMS_BASE, create_room, register_and_login


@pytest.fixture
def sync_client() -> Iterator[TestClient]:
    yield TestClient(app)
    # 每个用例结束后把 narrator 还原成默认实现，避免某个用例注入的 fake
    # 泄漏到后面的用例里（app 是全局单例，state 会跨用例存活）。
    app.state.narrator = FallbackNarrator()


def _join_ws(ws, player: dict) -> None:
    ws.send_json(
        {
            "type": "room.join",
            "playerId": player["playerId"],
            "payload": {"reconnectToken": player["reconnectToken"]},
        }
    )
    assert ws.receive_json()["type"] == "session.bound"


def _send_chat(ws, player: dict, text: str, client_message_id: str) -> None:
    ws.send_json(
        {
            "type": "chat.send",
            "playerId": player["playerId"],
            "payload": {"text": text, "clientMessageId": client_message_id},
        }
    )


def _submit_action(ws, player: dict, utterance: str) -> None:
    ws.send_json(
        {
            "type": "action.submit",
            "playerId": player["playerId"],
            "payload": {"utterance": utterance},
        }
    )


# ── 讨论区：落库 + 广播回显 ───────────────────────────


def test_chat_send_echoes_broadcast_with_full_payload(sync_client: TestClient) -> None:
    """chat.send 落库后广播 chat.message（发送者自己也靠广播回显，前端不做
    本地乐观插入）。payload 带齐渲染所需字段，时间戳带时区后缀（UtcDatetime
    的全项目约定）。"""
    token = register_and_login(sync_client, "chat_host")
    room = create_room(sync_client, token)

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        _send_chat(ws, room, "我们先去图书馆吧", "msg-1")
        envelope = next_game_event(ws)

    assert envelope["type"] == "chat.message"
    assert envelope["payload"]["text"] == "我们先去图书馆吧"
    assert envelope["payload"]["nickname"] == "房主"
    assert envelope["payload"]["playerId"] == room["playerId"]
    assert envelope["payload"]["clientMessageId"] == "msg-1"
    assert envelope["payload"]["sentAt"].endswith(("Z", "+00:00"))


def test_chat_send_is_idempotent_on_duplicate_client_message_id(
    sync_client: TestClient,
) -> None:
    """重连后重发同一条消息（相同 clientMessageId）：库里只有一行，第二次广播
    与第一次是同一条消息（messageId 相同），其他人不会看到重复气泡。"""
    token = register_and_login(sync_client, "idem_host")
    room = create_room(sync_client, token)

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        _send_chat(ws, room, "只发一次的消息", "dup-1")
        first = ws.receive_json()
        _send_chat(ws, room, "只发一次的消息", "dup-1")
        second = ws.receive_json()

    assert first["payload"]["messageId"] == second["payload"]["messageId"]

    history = sync_client.get(
        f"{ROOMS_BASE}/{room['roomId']}/messages",
        headers={"X-Reconnect-Token": room["reconnectToken"]},
    ).json()["data"]
    assert len(history) == 1


# ── action.submit：原话广播 + 叙事回复 ────────────────


def test_action_submit_broadcasts_utterance_then_narration(sync_client: TestClient) -> None:
    """action.submit 先广播发起者的**原话**（action.broadcast，修"聊天记录像
    被隔离"的 bug——此前原话只在发送方本地插入），再广播守秘人回复
    （narration.push）。双客户端的"对方也能看到"断言在 e2e（见文件头说明）。"""
    token = register_and_login(sync_client, "act_host")
    room = create_room(sync_client, token)

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        _submit_action(ws, room, "我推开吱呀作响的木门")
        echo = ws.receive_json()
        # exec/33 §5.4：原话之后先来一条「守秘人在忙」——前端的「正在思考」是
        # 本地在自己提交时点亮的，没发言的人看不到，分头时另一组因此是黑屏。
        busy_on = ws.receive_json()
        narration = ws.receive_json()
        busy_off = ws.receive_json()
        party = ws.receive_json()

    assert (busy_on["type"], busy_on["payload"]["busy"]) == ("keeper.busy", True)
    assert (busy_off["type"], busy_off["payload"]["busy"]) == ("keeper.busy", False)
    assert party["type"] == "party.update"
    assert echo["type"] == "action.broadcast"
    assert echo["payload"]["utterance"] == "我推开吱呀作响的木门"
    assert echo["payload"]["nickname"] == "房主"
    assert echo["payload"]["playerId"] == room["playerId"]
    assert narration["type"] == "narration.push"
    assert narration["payload"]["text"]


# ── 行动锁 ───────────────────────────────────────────


class _FailingNarrator(Narrator):
    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        raise RuntimeError("模拟 AI 服务超时/失败")


def test_lock_released_after_narrator_failure(sync_client: TestClient) -> None:
    """AI 调用失败后锁必须释放（finally 兜底），否则房间永久锁死——issue #107
    验收标准。失败只告诉发起者（error 不广播），其他人只看到原话没有回复。"""
    app.state.narrator = _FailingNarrator()

    token = register_and_login(sync_client, "fail_host")
    room = create_room(sync_client, token)

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)

        _submit_action(ws, room, "我尝试翻译古籍")
        assert ws.receive_json()["type"] == "action.broadcast"
        assert ws.receive_json() == {"type": "keeper.busy", "payload": {"busy": True}}
        failure = ws.receive_json()
        assert failure["type"] == "error"
        assert failure["payload"]["code"] == "INTERNAL_ERROR"
        # 失败后还会广播一条可见兜底叙事——聊天区不能静默，否则玩家以为断线。
        # error 只发给发起者，这条兜底是全房间可见的。
        assert ws.receive_json()["type"] == "narration.push"
        # 🔴 失败路径上「守秘人在忙」也必须熄灭（finally），否则它在所有人
        # 屏幕上永远亮着。这条用例正好守住那个 early return。
        assert ws.receive_json() == {"type": "keeper.busy", "payload": {"busy": False}}
        assert ws.receive_json()["type"] == "party.update"

        # 换一个能正常返回的 narrator，立刻重试——若锁没被释放，这里会收到
        # ACTION_IN_PROGRESS 而不是 action.broadcast。
        app.state.narrator = FallbackNarrator()
        _submit_action(ws, room, "我再次尝试翻译")
        assert ws.receive_json()["type"] == "action.broadcast"
        assert ws.receive_json() == {"type": "keeper.busy", "payload": {"busy": True}}
        assert ws.receive_json()["type"] == "narration.push"


def test_action_submit_private_is_accepted_and_echoed_to_self(sync_client: TestClient) -> None:
    """visibility="private"（exec/18 ⑥，P5.2c 落地）：不再回 NOT_IMPLEMENTED。

    这条只能验"发起者自己收得到、且没被当成错误拒掉"。**"其他人收不到"要两个
    真实 WS 客户端**，pytest 做不了（TestClient 每连接独立事件循环），受众计算
    本身由 `test_narration_fanout.py` 单测、端到端由 e2e 守。
    """
    token = register_and_login(sync_client, "priv_host")
    room = create_room(sync_client, token)

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        ws.send_json(
            {
                "type": "action.submit",
                "playerId": room["playerId"],
                "payload": {"utterance": "我偷偷摸他口袋", "visibility": "private"},
            }
        )
        envelope = next_game_event(ws)

    assert envelope["type"] == "action.broadcast"
    assert envelope["payload"]["utterance"] == "我偷偷摸他口袋"


def test_action_lock_semantics() -> None:
    """锁管理器本身的语义（不开 WS）：占用中拒绝、按房间隔离、释放后可得、
    超时自动过期、旧持有者不能误释放新持有者的锁（stale release 修复）。
    """
    manager = RoomActionLockManager()

    token1 = manager.try_acquire("room-1")
    assert token1 is not None
    assert manager.try_acquire("room-1") is None, "占用中必须拒绝"
    assert manager.try_acquire("room-2") is not None, "锁按房间隔离"

    manager.release("room-1", token1)
    token2 = manager.try_acquire("room-1")
    assert token2 is not None, "释放后必须可再次获取"

    # stale release：旧 token 不能释放新持有者的锁
    manager.release("room-1", token1)  # token1 已过期，不匹配 token2
    assert manager.try_acquire("room-1") is None, "旧 token release 不能误删新锁"
    manager.release("room-1", token2)  # 正确 token 才能释放

    # 超时兜底：把到期时间人为拨到过去，模拟"拿了锁但 release 没被走到"。
    manager._locks["room-1"] = (0.0, "stale-token")
    assert manager.try_acquire("room-1") is not None, "过期的锁必须能被抢占（防永久锁死）"

    manager.release("room-does-not-exist", "any-token")  # 释放不存在的锁是无害空操作


# ── check.roll / san.check.roll（两段式玩家掷骰，feat/keeper-agent）────


class _FakeCheckNarrator(Narrator):
    """narrate() 在这些用例里不应该被调用（只测 check.roll 这一条路径），
    resolve_check 桩死返回一个结果 + 一段结算叙事。"""

    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        raise AssertionError("这些用例不应该调用 narrate()")

    async def resolve_check(
        self,
        room_id: str,
        player_id: str,
        check_request_id: str,
        on_result: CheckResultCallback | None = None,
    ) -> NarrationOutcome:
        notice = CheckResultNotice(
            check_request_id=check_request_id,
            kind="skill",
            player_id=player_id,
            skill="侦查",
            rolled=42,
            target=60,
            level="成功",
        )
        return NarrationOutcome(text="你看清了痕迹。", check_results=[notice])


class _RejectingCheckNarrator(Narrator):
    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        raise AssertionError("这些用例不应该调用 narrate()")

    async def resolve_check(
        self,
        room_id: str,
        player_id: str,
        check_request_id: str,
        on_result: CheckResultCallback | None = None,
    ) -> NarrationOutcome:
        raise ValueError("没有这个待掷的检定（可能已被结算）")


def test_check_roll_broadcasts_result_and_narration(sync_client: TestClient) -> None:
    app.state.narrator = _FakeCheckNarrator()
    token = register_and_login(sync_client, "check_host")
    room = create_room(sync_client, token)

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        ws.send_json(
            {
                "type": "check.roll",
                "playerId": room["playerId"],
                "payload": {"checkRequestId": "chk-1"},
            }
        )
        result_envelope = next_game_event(ws)
        narration_envelope = next_game_event(ws)

    assert result_envelope["type"] == "check.result"
    assert result_envelope["payload"]["checkRequestId"] == "chk-1"
    assert result_envelope["payload"]["rollValue"] == 42
    assert result_envelope["payload"]["result"] == "成功"
    assert narration_envelope["type"] == "narration.push"
    assert narration_envelope["payload"]["text"] == "你看清了痕迹。"


def test_check_roll_not_implemented_without_keeper(sync_client: TestClient) -> None:
    """默认 FallbackNarrator 没有"待掷检定"概念——check.roll 收到 NOT_IMPLEMENTED，
    而不是让请求悬空等不到任何回应。"""
    token = register_and_login(sync_client, "check_fallback_host")
    room = create_room(sync_client, token)

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        ws.send_json(
            {
                "type": "check.roll",
                "playerId": room["playerId"],
                "payload": {"checkRequestId": "chk-1"},
            }
        )
        envelope = next_game_event(ws)

    assert envelope["type"] == "error"
    assert envelope["payload"]["code"] == "NOT_IMPLEMENTED"


def test_san_check_roll_unknown_id_returns_check_not_pending(sync_client: TestClient) -> None:
    app.state.narrator = _RejectingCheckNarrator()
    token = register_and_login(sync_client, "check_reject_host")
    room = create_room(sync_client, token)

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        ws.send_json(
            {
                "type": "san.check.roll",
                "playerId": room["playerId"],
                "payload": {"checkRequestId": "no-such"},
            }
        )
        envelope = next_game_event(ws)

    assert envelope["type"] == "error"
    assert envelope["payload"]["code"] == "CHECK_NOT_PENDING"


# ── 历史消息 REST ────────────────────────────────────


def test_messages_pagination_with_before_cursor(sync_client: TestClient) -> None:
    token = register_and_login(sync_client, "page_host")
    room = create_room(sync_client, token)
    headers = {"X-Reconnect-Token": room["reconnectToken"]}

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        for i in range(3):
            _send_chat(ws, room, f"第{i + 1}条", f"pg-{i}")
            ws.receive_json()

    page1 = sync_client.get(
        f"{ROOMS_BASE}/{room['roomId']}/messages", params={"limit": 2}, headers=headers
    ).json()["data"]
    assert [m["text"] for m in page1] == ["第3条", "第2条"]  # 倒序，最新在前

    page2 = sync_client.get(
        f"{ROOMS_BASE}/{room['roomId']}/messages",
        params={"limit": 2, "before": page1[-1]["messageId"]},
        headers=headers,
    ).json()["data"]
    assert [m["text"] for m in page2] == ["第1条"]


def test_messages_rejects_non_member(sync_client: TestClient) -> None:
    token = register_and_login(sync_client, "member_host")
    room = create_room(sync_client, token)
    # 另一个房间的人拿自己的 reconnect_token 来查这个房间 → 拒绝
    other_token = register_and_login(sync_client, "outsider")
    other_room = create_room(sync_client, other_token)

    response = sync_client.get(
        f"{ROOMS_BASE}/{room['roomId']}/messages",
        headers={"X-Reconnect-Token": other_room["reconnectToken"]},
    )
    assert response.status_code == 403


# ── 退房清理 / 复盘纯净 ──────────────────────────────


def test_end_game_clears_chat_and_replay_stays_clean(sync_client: TestClient) -> None:
    """房主结束游戏后聊天记录被清空；聊天从头到尾不出现在 replay 里
    （issue #107 验收标准：聊天是临时工作记忆，不进复盘）。"""
    from tests.test_ws import advance_to_building, complete_character

    token = register_and_login(sync_client, "end_host")
    room = create_room(sync_client, token)
    headers = {"X-Reconnect-Token": room["reconnectToken"]}
    advance_to_building(sync_client, room)
    complete_character(sync_client, room["roomId"], room["reconnectToken"])

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        ws.send_json({"type": "game.start", "playerId": room["playerId"], "payload": {}})
        ws.receive_json()  # 开场 narration
        _send_chat(ws, room, "这句话不该进复盘", "end-1")
        ws.receive_json()

    # 聊天在 end 之前查得到
    assert (
        len(
            sync_client.get(f"{ROOMS_BASE}/{room['roomId']}/messages", headers=headers).json()[
                "data"
            ]
        )
        == 1
    )

    end_response = sync_client.post(f"{ROOMS_BASE}/{room['roomId']}/end", headers=headers)
    assert end_response.status_code == 200

    # end 之后聊天被清空
    assert (
        sync_client.get(f"{ROOMS_BASE}/{room['roomId']}/messages", headers=headers).json()["data"]
        == []
    )

    # replay 里从头到尾没有聊天内容
    replay = sync_client.get(f"{ROOMS_BASE}/{room['roomId']}/replay", headers=headers).json()[
        "data"
    ]
    assert "这句话不该进复盘" not in str(replay)


# ── 重连补发待掷检定（真人实测 exec/23 #56）────────


def test_reconnect_resends_the_pending_check_card(sync_client: TestClient) -> None:
    """🔴 刷新页面/重启后端之后，那张检定卡片必须回来。

    真人实测当场复现：队列落库（exec/24 §8.1）保证了**服务端不会忘**，但
    `check.request` 只在裁决那一刻实时推过一次——重连的人再也收不到它，而
    `narrate` 的守卫会一直挡住新一轮，对局停在"守秘人等你掷骰、你屏幕上却
    没有骰子"的死角。两半都做了，重启才真的能接着玩。

    断言落在 `session.bound` **之后紧接着**收到 check.request 上——顺序错了
    （比如放在别的握手步骤后面）客户端可能已经开始渲染，卡片就插不进去了。
    """
    import asyncio

    from app.controller import ws as ws_controller
    from app.core.keeper.runtime.pending import PendingDecision, pending_decision_manager

    # 🔴 **不要** `from tests.conftest import TestSessionLocal`——conftest 顶部
    # 明文警告过：那会把 conftest 当成另一个模块再导入一次、连带新建一个引擎，
    # 表建在旧引擎上，结果是 `no such table`（我照着踩了一次）。
    # conftest 已经把 ws 模块的会话工厂重绑到测试库，直接用它就是同一个引擎。
    session_factory = ws_controller.async_session_factory

    token = register_and_login(sync_client, "reconnect_host")
    room = create_room(sync_client, token)

    async def _seed() -> None:
        async with session_factory() as db:
            await pending_decision_manager.add(
                db,
                room["roomId"],
                [
                    PendingDecision.roll(
                        decision_id="chk-survived",
                        kind="skill",
                        room_id=room["roomId"],
                        player_id=room["playerId"],
                        player_nickname="李明轩",
                        skill="格斗：斗殴",
                        loss_on_success="0",
                        loss_on_failure="0",
                        reason="他抬手格挡",
                    )
                ],
            )
            await db.commit()

    asyncio.run(_seed())

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        ws.send_json(
            {
                "type": "room.join",
                "playerId": room["playerId"],
                "payload": {"reconnectToken": room["reconnectToken"]},
            }
        )
        # 🔴 握手之后立刻发一条**必然有回复**的消息，再连读两条。
        # 不这么做的话，功能一旦回退这个用例会**挂住**（等一条永远不来的
        # 消息）而不是变红——CI 里挂住比失败更糟，因为没人知道它在等什么。
        _send_chat(ws, room, "在吗", "cid-reconnect")
        first = ws.receive_json()
        second = ws.receive_json()

    assert first["type"] == "session.bound"
    # 补发要排在聊天回执**之前**：它属于握手的一部分，晚于客户端已经开始
    # 处理别的消息就可能插不进渲染
    assert second["type"] == "check.request", f"握手没有补发检定卡片：{second}"
    assert second["payload"]["checkRequestId"] == "chk-survived"
    assert second["payload"]["skill"] == "格斗：斗殴"


def test_reconnect_does_not_resend_the_other_groups_card(sync_client: TestClient) -> None:
    """🔴 exec/33 §10 #78：**补发不按受众裁 = 泄露**，双人真机一局复现两次。

    阿福在屋后掷潜行，阿贵**刷新一下页面**就连那张卡带理由文本一起收到了。
    首发是裁过的（`_send_to_colocated`），补发这一半却把房间里所有卡片原样
    推给刚绑定的连接——同一件事的两头，一头做了一头没做。

    ⚠️ 断言"没收到"必须让功能回退时**变红而不是挂住**：握手后立刻发一条必然
    有回复的消息，断言下一条就是那个回执（中间挤进卡片就是顺序不对）。
    """
    import asyncio

    from sqlalchemy import select

    from app.controller import ws as ws_controller
    from app.core.keeper.runtime.location_state import PLAYER_LOCATION_KEY
    from app.core.keeper.runtime.pending import PendingDecision, pending_decision_manager
    from app.models.room import Room

    session_factory = ws_controller.async_session_factory

    host_token = register_and_login(sync_client, "leak_host")
    room = create_room(sync_client, host_token)
    guest_token = register_and_login(sync_client, "leak_guest")
    guest = sync_client.post(
        f"{ROOMS_BASE}/{room['roomCode']}/join",
        json={"nickname": "阿贵"},
        headers={"Authorization": f"Bearer {guest_token}"},
    ).json()["data"]
    assert guest["playerId"] != room["playerId"]

    async def _seed() -> None:
        async with session_factory() as db:
            db_room = await db.scalar(select(Room).where(Room.id == room["roomId"]))
            assert db_room is not None, "摆前置状态必须真的摆上：查不到就该当场炸"
            db_room.keeper_state = {
                PLAYER_LOCATION_KEY: (
                    f"{room['playerId']}@backyard, {guest['playerId']}@front-porch"
                )
            }
            await pending_decision_manager.add(
                db,
                room["roomId"],
                [
                    PendingDecision.roll(
                        decision_id="chk-not-yours",
                        kind="skill",
                        room_id=room["roomId"],
                        player_id=room["playerId"],
                        player_nickname="阿福",
                        skill="潜行",
                        loss_on_success="0",
                        loss_on_failure="0",
                        reason="避免被科比特或邻居发现",
                    )
                ],
            )
            await db.commit()

    asyncio.run(_seed())

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={guest_token}") as ws:
        ws.send_json(
            {
                "type": "room.join",
                "playerId": guest["playerId"],
                "payload": {"reconnectToken": guest["reconnectToken"]},
            }
        )
        _send_chat(ws, guest, "在吗", "cid-leak")
        first = ws.receive_json()
        second = ws.receive_json()

    assert first["type"] == "session.bound"
    assert second["type"] == "chat.message", f"另一组的检定卡片漏给了阿贵：{second}"
    assert "科比特" not in str(second)


def test_reconnect_still_resends_your_own_card_while_split(sync_client: TestClient) -> None:
    """上一条不许把功能整个关掉：**自己那张**卡分头时照样要补发（#56 仍然成立）。"""
    import asyncio

    from sqlalchemy import select

    from app.controller import ws as ws_controller
    from app.core.keeper.runtime.location_state import PLAYER_LOCATION_KEY
    from app.core.keeper.runtime.pending import PendingDecision, pending_decision_manager
    from app.models.room import Room

    session_factory = ws_controller.async_session_factory

    host_token = register_and_login(sync_client, "split_host")
    room = create_room(sync_client, host_token)
    guest_token = register_and_login(sync_client, "split_guest")
    guest = sync_client.post(
        f"{ROOMS_BASE}/{room['roomCode']}/join",
        json={"nickname": "阿贵"},
        headers={"Authorization": f"Bearer {guest_token}"},
    ).json()["data"]

    async def _seed() -> None:
        async with session_factory() as db:
            db_room = await db.scalar(select(Room).where(Room.id == room["roomId"]))
            assert db_room is not None
            db_room.keeper_state = {
                PLAYER_LOCATION_KEY: (
                    f"{room['playerId']}@backyard, {guest['playerId']}@front-porch"
                )
            }
            await pending_decision_manager.add(
                db,
                room["roomId"],
                [
                    PendingDecision.roll(
                        decision_id="chk-mine",
                        kind="skill",
                        room_id=room["roomId"],
                        player_id=room["playerId"],
                        player_nickname="阿福",
                        skill="潜行",
                        loss_on_success="0",
                        loss_on_failure="0",
                        reason="避免被科比特或邻居发现",
                    )
                ],
            )
            await db.commit()

    asyncio.run(_seed())

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={host_token}") as ws:
        ws.send_json(
            {
                "type": "room.join",
                "playerId": room["playerId"],
                "payload": {"reconnectToken": room["reconnectToken"]},
            }
        )
        _send_chat(ws, room, "在吗", "cid-mine")
        first = ws.receive_json()
        second = ws.receive_json()

    assert first["type"] == "session.bound"
    assert second["type"] == "check.request", f"分头时自己的卡片没补发：{second}"
    assert second["payload"]["checkRequestId"] == "chk-mine"


def test_reconnect_sends_nothing_extra_when_the_queue_is_empty(
    sync_client: TestClient,
) -> None:
    """没有待掷检定时，握手**逐字不变**——不能凭空多一条消息出来。"""
    token = register_and_login(sync_client, "reconnect_clean")
    room = create_room(sync_client, token)

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        # 再发一条别的，能原样收到回执就说明中间没有多余消息挤进来
        ws.send_json(
            {
                "type": "room.join",
                "playerId": room["playerId"],
                "payload": {"reconnectToken": room["reconnectToken"]},
            }
        )
        assert ws.receive_json()["type"] == "session.bound"
