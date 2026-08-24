"""一局分多个晚上跑完（`exec/46` B3）：散会 / 续跑 / 「上次讲到哪」。

## 这里守的是接线，不只是函数

判据来自 2026-08-20 那次教训：**一整个特性可以只差一根接线就完全不存在**
（玩家结束权的 11 条用例全是 service 层直调，于是「发言那条路根本没人推那张
卡」在 2234 条绿灯下活了一整天）。所以散会那条**必须走 WS**，一路验到
「行动真的被挡回」，不能只调 `adjourn_session` 看它返回什么。
"""

import sqlite3
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.testclient import TestClient

from app.core.narration.fallback import FallbackNarrator
from app.core.table_state import PHASE_ADJOURNED, table_is_open
from app.main import app
from app.models.room import Room
from tests.test_ws import ROOMS_BASE, create_room, join_as, register_and_login


@pytest.fixture
def sync_client():
    yield TestClient(app)
    app.state.narrator = FallbackNarrator()


def _room_key(room_id: str) -> str:
    # 🔴 UUID 在库里**不带连字符**
    return room_id.replace("-", "")


def _set_phase(room_id: str, phase: str) -> None:
    from conftest import _TEST_DB_PATH

    conn = sqlite3.connect(_TEST_DB_PATH)
    try:
        changed = conn.execute(
            "UPDATE rooms SET phase = ? WHERE id = ?", (phase, _room_key(room_id))
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    # 摆状态的 helper 改了 0 行必须炸（`exec/33` 那条教训）
    assert changed == 1, "前置没摆上——UPDATE 改了 0 行"


def _read_phase(room_id: str) -> str:
    from conftest import _TEST_DB_PATH

    conn = sqlite3.connect(_TEST_DB_PATH)
    try:
        row = conn.execute("SELECT phase FROM rooms WHERE id = ?", (_room_key(room_id),)).fetchone()
    finally:
        conn.close()
    return row[0]


def _sessions(room_id: str) -> list[tuple]:
    from conftest import _TEST_DB_PATH

    conn = sqlite3.connect(_TEST_DB_PATH)
    try:
        return conn.execute(
            "SELECT status, started_at, ended_at FROM room_sessions "
            "WHERE room_id = ? ORDER BY started_at",
            (_room_key(room_id),),
        ).fetchall()
    finally:
        conn.close()


def _join_ws(ws, player: dict) -> None:
    ws.send_json(
        {
            "type": "room.join",
            "playerId": player["playerId"],
            "payload": {"reconnectToken": player["reconnectToken"]},
        }
    )
    assert ws.receive_json()["type"] == "session.bound"


def _adjourn(ws, player: dict, adjourned: bool) -> None:
    ws.send_json(
        {
            "type": "room.adjourn",
            "playerId": player["playerId"],
            "payload": {"adjourned": adjourned},
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


# ── 判据本身 ──────────────────────────────────────


def test_table_is_open_covers_both_kinds_of_stop() -> None:
    """🔴 两种停都要挡住。**分别造反例**：只验一种的话，把 `table_is_open`
    退化成 `not room.paused`（或退化成只看 phase）各有一半的测试照样绿。"""
    open_room = Room(phase="InGame", paused=False)
    resting = Room(phase="InGame", paused=True)
    adjourned = Room(phase=PHASE_ADJOURNED, paused=False)

    assert table_is_open(open_room) is True
    assert table_is_open(resting) is False, "「大家在休息」没挡住"
    assert table_is_open(adjourned) is False, "「今晚收工」没挡住"


# ── 接线：散会之后那条路真的走不通 ──────────────────


def test_adjourning_blocks_actions_and_resuming_lets_them_through(
    sync_client: TestClient,
) -> None:
    """🔴 一路走 WS，验到「行动被挡回」为止。

    只调 service 看它把 phase 改成了 `Adjourned` 是不够的——那正是 2026-08-20
    那条教训的形状：判据对了、卡建出来了，而**推送那条路一次都没走过**。
    """
    token = register_and_login(sync_client, "adjourn_host")
    room = create_room(sync_client, token)
    _set_phase(room["roomId"], "InGame")

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)

        _adjourn(ws, room, True)
        adjourned_evt = ws.receive_json()
        _submit_action(ws, room, "我推开门")
        refused = ws.receive_json()

        _adjourn(ws, room, False)
        resumed_evt = ws.receive_json()
        _submit_action(ws, room, "我推开门")
        echo = ws.receive_json()

    assert adjourned_evt["type"] == "room.adjourned"
    assert adjourned_evt["payload"]["adjourned"] is True
    assert adjourned_evt["payload"]["byNickname"] == "房主"

    assert refused["type"] == "error"
    assert refused["payload"]["code"] == "CONFLICT"
    # 🔴 断言选得连反例都装不下：休息那条的文案是「大家在休息」，
    # 两条错误码相同，只有文案能区分是哪一道门挡的。
    assert "收工" in refused["payload"]["message"]

    assert resumed_evt["payload"]["adjourned"] is False
    assert echo["type"] == "action.broadcast", "续跑之后行动仍然走不通"


def test_only_the_host_can_adjourn(sync_client: TestClient) -> None:
    """🔴 跟「先休息一下」相反：那个任何人都能按，这个只有房主。

    两档粒度共用一套权限就是「两件事共用一个开关」。
    """
    token = register_and_login(sync_client, "adjourn_owner")
    room = create_room(sync_client, token)
    guest = join_as(sync_client, room["roomCode"], "adjourn_guest")
    _set_phase(room["roomId"], "InGame")

    guest_token = register_and_login(sync_client, "adjourn_guest_ws")
    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={guest_token}") as ws:
        _join_ws(ws, guest)
        _adjourn(ws, guest, True)
        refused = ws.receive_json()

    assert refused["type"] == "error"
    assert refused["payload"]["code"] == "FORBIDDEN"
    assert _read_phase(room["roomId"]) == "InGame", "非房主把房间收工了"


# ── 场次记录 ──────────────────────────────────────


def test_each_gathering_gets_its_own_session_row(sync_client: TestClient) -> None:
    """散会 → 续跑之后应该是**两行**：上一场结掉、新的一场开着。

    这是「这一局聚过几次」唯一的依据。
    """
    token = register_and_login(sync_client, "session_rows")
    room = create_room(sync_client, token)
    _set_phase(room["roomId"], "InGame")

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        _adjourn(ws, room, True)
        first = ws.receive_json()
        _adjourn(ws, room, False)
        second = ws.receive_json()

    # 🔴 这个房间没走过 `game.start`（前置是直接摆的 phase），所以开局那一行
    #    不存在——散会时 `close_session` 没有可结的场次，**不该凭空补一行**。
    rows = _sessions(room["roomId"])
    assert [r[0] for r in rows] == ["active"], f"场次记录不对：{rows}"
    assert rows[0][2] is None

    assert first["payload"]["sessionCount"] == 0
    assert second["payload"]["sessionCount"] == 1


def test_starting_the_story_opens_the_first_session(sync_client: TestClient) -> None:
    """正式开局 = 第一次聚会。**这条守的是接线**：`open_session` 写得再对，
    没人在 `start_game` 里调它，「聚过几次」就永远是 0。"""
    from tests.test_ws import complete_character

    token = register_and_login(sync_client, "session_start")
    room = create_room(sync_client, token)
    headers = {"X-Reconnect-Token": room["reconnectToken"]}
    modules = sync_client.get("/api/v1/modules", headers={"Authorization": f"Bearer {token}"})
    module_id = modules.json()["data"][0]["id"]
    sync_client.post(
        f"{ROOMS_BASE}/{room['roomId']}/module",
        json={"moduleId": module_id},
        headers=headers,
    )
    sync_client.post(f"{ROOMS_BASE}/{room['roomId']}/start-story", headers=headers)
    complete_character(sync_client, room["roomId"], room["reconnectToken"])

    with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
        _join_ws(ws, room)
        ws.send_json({"type": "game.start", "playerId": room["playerId"], "payload": {}})
        for _ in range(6):
            evt = ws.receive_json()
            if evt["type"] in ("error", "narration.push"):
                break

    rows = _sessions(room["roomId"])
    assert [r[0] for r in rows] == ["active"], f"开局没有开出第一场聚会：{rows}"
    assert rows[0][1] is not None, "第一场聚会没有起点"


def test_ending_the_game_works_from_adjourned(sync_client: TestClient) -> None:
    """🔴 散会态也能直接 `/end`：上周打到一半收了工，这周决定不跑了。

    逼房主先「继续」再「结束」是没道理的。加第四态时 `end_game` 那句
    `!= "InGame"` 是**逐个列出的地方**。
    """
    token = register_and_login(sync_client, "end_from_adjourned")
    room = create_room(sync_client, token)
    _set_phase(room["roomId"], PHASE_ADJOURNED)

    response = sync_client.post(
        f"{ROOMS_BASE}/{room['roomId']}/end",
        headers={"X-Reconnect-Token": room["reconnectToken"]},
    )
    assert response.status_code == 200, response.text
    assert _read_phase(room["roomId"]) == "Completed"


# ── 「上次讲到哪」 ────────────────────────────────


def test_last_session_reports_nothing_before_the_first_adjournment(
    sync_client: TestClient,
) -> None:
    """一次都没散过会 ⇒ `recapText` 是 null。

    🔴 **不许拿占位文案填**：`null` 的含义是"还没有"，一句"上次你们大有斩获"
    会被人当成真的接着往下玩。
    """
    token = register_and_login(sync_client, "last_session_empty")
    room = create_room(sync_client, token)

    response = sync_client.get(f"{ROOMS_BASE}/{room['roomId']}/last-session")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["recapText"] is None
    assert data["sessionCount"] == 0
    assert data["adjourned"] is False


@pytest.mark.asyncio
async def test_session_events_never_fall_back_to_the_whole_game() -> None:
    """🔴 场次没有起点时返回**空**，不退化成"取全部事件"。

    退化的后果不是少一段，是把十个晚上的事当成一个晚上讲——而它长得跟正常
    输出一模一样，没有任何东西会变红。
    """
    from app.models.replay import RoomSession
    from app.service.session_recap import _session_events

    # 🔴 `db` 故意传 None：它一旦退化成"去查库"就会当场 AttributeError，
    #    比断言列表长度更难糊弄过去。
    session = RoomSession(room_id="whatever", status="ended", started_at=None)
    assert await _session_events(cast(AsyncSession, None), session) == []
