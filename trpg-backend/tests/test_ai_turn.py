"""AI 玩家第三层：行动决策（exec/21）。

这份用例的重点**不是"它说得好不好"**（那是模型的事，测不了），而是三件
结构性的事：

1. **它的视角是被裁过的**——分头时队友那半段历史，它的上下文里根本没有；
2. **它没有特权路径**——那句话走跟真人相同的 action.submit，并进同一轮；
3. **它坏掉时全桌照常**——决策失败/超时一律退化成沉默，不炸真人的回合。
"""

from typing import Any

import pytest
from starlette.testclient import TestClient
from test_ws import ROOMS_BASE, create_room, register_and_login

from app.core.ai_actor import AiActor, AiPlayerIntent, build_view
from app.core.keeper.history import HistoryLine
from app.main import app
from app.service.ai_turn import collect_ai_submissions


@pytest.fixture
def sync_client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def stub_narrator():  # noqa: ANN201
    """🔴 钉死叙事器，别让开发机 `.env` 参与选择。

    配了 `DEEPSEEK_API_KEY` 的机器上 `app.state.narrator` 是真的 keeper，这几条
    用例就会去连真实模组目录、报"房间未绑定可玩模组"。这里要验的是**接线**
    （AI 的话有没有并进同一轮），叙事内容用占位回显反而更好断言。
    """
    from app.core.narration.fallback import FallbackNarrator

    previous = app.state.narrator
    app.state.narrator = FallbackNarrator()
    yield
    app.state.narrator = previous


class _Character:
    """够 `build_view`/`_character_sheet` 用的最小角色卡替身。"""

    name = "阿铁"
    occupation = "记者"
    age = 30
    attributes = {"STR": 50, "EDU": 70}
    skills = {"侦察": 65, "话术": 40, "图书馆使用": 70}
    derived_stats = {"HP": 10, "SAN": 55}


# ── 有限视角 ───────────────────────────────────────


def test_view_only_contains_what_this_player_experienced() -> None:
    """🔴 这层保密的全部强度就在这一条断言上。

    历史里有一行只发给了别人（分头时地下室那段）。AI 的视角必须**读不出来**
    ——不是"提示它别用"，是它的 prompt 里压根没有那串字。
    """
    lines = [
        HistoryLine("守秘人：门厅里积着灰。", None),
        HistoryLine("守秘人：地下室的铁柜上刻着一个名字。", frozenset({"human-1"})),
        HistoryLine("张家豪：我去翻抽屉。", frozenset({"human-1"})),
    ]
    view = build_view(
        character=_Character(), history_lines=lines, player_id="ai-1", roster=["张家豪"]
    )
    assert "门厅里积着灰" in view
    assert "铁柜" not in view
    assert "我去翻抽屉" not in view


def test_view_includes_own_sheet_and_tablemates() -> None:
    view = build_view(
        character=_Character(), history_lines=[], player_id="ai-1", roster=["张家豪", "凌铭辉"]
    )
    assert "阿铁" in view
    assert "记者" in view
    assert "图书馆使用 70" in view  # 技能按值排序取前几项
    assert "张家豪" in view and "凌铭辉" in view


def test_view_survives_an_empty_history() -> None:
    """开局第一轮：历史是空的，不能渲染成空块让模型自己脑补。"""
    view = build_view(character=_Character(), history_lines=[], player_id="ai-1", roster=[])
    assert "游戏刚开始" in view


# ── 决策的兜底：任何失败都退化成沉默 ────────────────


class _FakeCompletions:
    def __init__(self, payload: str | Exception) -> None:
        self._payload = payload
        self.calls = 0

    async def create(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls += 1
        if isinstance(self._payload, Exception):
            raise self._payload

        class _Message:
            content = self._payload

        class _Choice:
            message = _Message()

        class _Response:
            choices = [_Choice()]

        return _Response()


def _actor_with(payload: str | Exception) -> tuple[AiActor, _FakeCompletions]:
    actor = AiActor("fake-key")
    fake = _FakeCompletions(payload)
    # 假客户端只需要 `.chat.completions.create`，不必是真的 TapedClient
    client: Any = type("C", (), {"chat": type("Chat", (), {"completions": fake})()})()
    actor._client = client
    return actor, fake


async def test_decide_returns_the_utterance() -> None:
    actor, _ = _actor_with('{"thinking": "跟着看看", "act": true, "utterance": "我去翻抽屉"}')
    intent = await actor.decide("视角")
    assert intent.act is True
    assert intent.utterance == "我去翻抽屉"


async def test_decide_can_stay_silent() -> None:
    """沉默是一等公民：真人玩家大多数回合也只是听着。"""
    actor, _ = _actor_with('{"thinking": "这轮轮不到我", "act": false, "utterance": ""}')
    intent = await actor.decide("视角")
    assert intent.act is False


async def test_act_true_with_empty_utterance_degrades_to_silence() -> None:
    """说了要行动却没给内容 → 沉默。空串走进 action.submit 会在所有人屏幕上
    广播一个空气泡。"""
    actor, _ = _actor_with('{"thinking": "", "act": true, "utterance": "   "}')
    intent = await actor.decide("视角")
    assert intent.act is False


async def test_long_utterance_is_clipped_by_code() -> None:
    """长度靠代码硬裁，不靠 prompt 自觉——AI 说长了会盖过真人的戏份。"""
    actor, _ = _actor_with('{"thinking": "", "act": true, "utterance": "' + "我" * 200 + '"}')
    intent = await actor.decide("视角")
    assert len(intent.utterance) == 60


async def test_invalid_json_degrades_to_silence() -> None:
    actor, _ = _actor_with("这不是 JSON")
    intent = await actor.decide("视角")
    assert intent.act is False


async def test_network_failure_degrades_to_silence_instead_of_raising() -> None:
    """🔴 它是补位的，桌上还有真人在等。异常冒到 _run_turn 会让**真人的**
    那一轮也一起失败。"""
    actor, _ = _actor_with(TimeoutError("timeout"))
    intent = await actor.decide("视角")
    assert intent.act is False


# ── 收集：没配 key / 没有 AI 时一次多余的查询都不做 ──


class _StubActor:
    def __init__(self, intent: AiPlayerIntent) -> None:
        self._intent = intent
        self.views: list[str] = []

    async def decide(self, view: str) -> AiPlayerIntent:
        self.views.append(view)
        return self._intent


async def test_collect_without_actor_returns_nothing(db_session) -> None:  # noqa: ANN001
    assert await collect_ai_submissions(db_session, "room-x", None) == []


# ── WS 接线：AI 的话并进同一轮，走同一条路径 ─────────


def _ai_room(client: TestClient, account: str) -> tuple[dict, str]:
    token = register_and_login(client, account)
    room = create_room(client, token)
    response = client.post(
        f"{ROOMS_BASE}/{room['roomId']}/ai-players",
        headers={"X-Reconnect-Token": room["reconnectToken"]},
    )
    assert response.status_code == 201
    return room, token


def test_ai_teammate_speaks_in_discussion_not_to_the_keeper(
    sync_client: TestClient,
    stub_narrator,  # noqa: ANN001
) -> None:
    """🔴 exec/25 #60：AI 队友的话进**讨论区**，且**不进守秘人这一轮**。

    第一版让它走跟真人完全相同的 `action.submit`，本意是防作弊。真人实测暴露
    了这条原则选错了方向——玩家问「我们能直接去他的地下室吗」，AI 答「先别急，
    看看动静」，裁决器收到两条**等权**发言、按其中的行动宣言推进了世界：
    **AI 把玩家的提问变成了一次行动。**

    最后那条断言（AI 的话**不在**叙事文本里）是这次改动的**全部保证**：占位
    叙事器原样回显本轮合并后的宣告，AI 那句要是混进去了，它必然出现在文本里。
    把 `_post_ai_suggestion` 改回 `_ingest_utterance` 这条就会红。
    """
    room, token = _ai_room(sync_client, "ai_turn_host")
    previous = app.state.ai_actor
    app.state.ai_actor = _StubActor(AiPlayerIntent(act=True, utterance="后门那边我们还没看过"))
    try:
        with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
            ws.send_json(
                {
                    "type": "room.join",
                    "playerId": room["playerId"],
                    "payload": {"reconnectToken": room["reconnectToken"]},
                }
            )
            ws.receive_json()  # session.bound
            ws.send_json(
                {
                    "type": "action.submit",
                    "playerId": room["playerId"],
                    "payload": {"utterance": "检查门锁"},
                }
            )
            first = ws.receive_json()
            second = ws.receive_json()
            third = ws.receive_json()
    finally:
        app.state.ai_actor = previous

    assert first["type"] == "action.broadcast"
    assert first["payload"]["utterance"] == "检查门锁"
    # AI 走讨论区通道，不是 action.broadcast
    assert second["type"] == "chat.message"
    assert second["payload"]["text"] == "后门那边我们还没看过"
    assert second["payload"]["playerId"] != room["playerId"]
    # 仍然只有一段守秘人回应
    assert third["type"] == "narration.push"
    assert "检查门锁" in third["payload"]["text"]
    # 🔴 核心保证：AI 说的话**够不到**裁决器
    assert "后门那边我们还没看过" not in third["payload"]["text"]


def test_silent_ai_teammate_changes_nothing(sync_client: TestClient, stub_narrator) -> None:  # noqa: ANN001
    """AI 选择不说话时，这一轮跟没有它**逐字相同**——不留空广播、不多一轮。"""
    room, token = _ai_room(sync_client, "ai_turn_silent")
    previous = app.state.ai_actor
    app.state.ai_actor = _StubActor(AiPlayerIntent(act=False))
    try:
        with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
            ws.send_json(
                {
                    "type": "room.join",
                    "playerId": room["playerId"],
                    "payload": {"reconnectToken": room["reconnectToken"]},
                }
            )
            ws.receive_json()
            ws.send_json(
                {
                    "type": "action.submit",
                    "playerId": room["playerId"],
                    "payload": {"utterance": "检查门锁"},
                }
            )
            first = ws.receive_json()
            second = ws.receive_json()
    finally:
        app.state.ai_actor = previous

    assert first["type"] == "action.broadcast"
    assert second["type"] == "narration.push"
    assert second["payload"]["text"].count("检查门锁") == 1


def test_broken_ai_teammate_does_not_break_the_human_turn(
    sync_client: TestClient,
    stub_narrator,  # noqa: ANN001
) -> None:
    """🔴 AI 的决策器整个抛异常，真人那一轮照样跑完。"""

    class _ExplodingActor:
        async def decide(self, view: str) -> AiPlayerIntent:
            raise RuntimeError("模型挂了")

    room, token = _ai_room(sync_client, "ai_turn_broken")
    previous = app.state.ai_actor
    app.state.ai_actor = _ExplodingActor()
    try:
        with sync_client.websocket_connect(f"/ws/{room['roomId']}?token={token}") as ws:
            ws.send_json(
                {
                    "type": "room.join",
                    "playerId": room["playerId"],
                    "payload": {"reconnectToken": room["reconnectToken"]},
                }
            )
            ws.receive_json()
            ws.send_json(
                {
                    "type": "action.submit",
                    "playerId": room["playerId"],
                    "payload": {"utterance": "检查门锁"},
                }
            )
            first = ws.receive_json()
            second = ws.receive_json()
    finally:
        app.state.ai_actor = previous

    assert first["type"] == "action.broadcast"
    assert second["type"] == "narration.push"
    assert "检查门锁" in second["payload"]["text"]
