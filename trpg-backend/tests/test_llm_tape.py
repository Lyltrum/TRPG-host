"""LLM 磁带（exec/14 P0）：录制 / 回放 / 漂移 / 版权守卫。

这些用例不打网络——录制路径用一个假的 openai 客户端替身验证「录了什么」，
回放路径验证「同样的磁带重放两次结果完全一致」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.core.llm_tape import (
    COMMITTABLE_SCENARIOS,
    Tape,
    TapedClient,
    TapeExhausted,
    TapeMismatch,
    recording,
    replaying,
)

COMMITTED_TAPES_DIR = Path(__file__).parent / "tapes"


@dataclass
class _FakeMessage:
    content: str | None


@dataclass
class _FakeChoice:
    message: _FakeMessage
    finish_reason: str | None


@dataclass
class _FakeResponse:
    choices: list[_FakeChoice]


class _FakeCompletions:
    """假的 openai completions：按预设脚本依次返回，并记下收到的请求。"""

    def __init__(self, script: list[str]) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    async def create(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        return _FakeResponse(
            choices=[_FakeChoice(message=_FakeMessage(self._script.pop(0)), finish_reason="stop")]
        )


class _FakeChat:
    def __init__(self, script: list[str]) -> None:
        self.completions = _FakeCompletions(script)


class _FakeInner:
    def __init__(self, script: list[str]) -> None:
        self.chat = _FakeChat(script)


def _client(script: list[str]) -> TapedClient:
    return TapedClient(_FakeInner(script))


def _inner_calls(client: TapedClient) -> list[dict]:
    """拿到底层假客户端真正收到的请求（`client.chat.completions` 已经是包装层）。"""
    return client._inner.chat.completions.calls


async def _two_calls(client: TapedClient) -> list[str]:
    a = await client.chat.completions.create(
        tape_kind="adjudicate",
        model="m",
        messages=[{"role": "user", "content": "玩家：我翻找书桌"}],
        response_format={"type": "json_object"},
    )
    b = await client.chat.completions.create(
        tape_kind="narrate",
        model="m",
        messages=[{"role": "user", "content": "写一段"}],
        temperature=0.8,
    )
    return [a.choices[0].message.content, b.choices[0].message.content]


@pytest.mark.asyncio
async def test_record_then_replay_returns_same_outputs(tmp_path: Path) -> None:
    path = tmp_path / "tape.json"
    client = _client(['{"checks": []}', "书桌抽屉里空空如也。"])

    with recording(path, scenario="tests/fixtures/keeper_module.json"):
        recorded = await _two_calls(client)

    assert path.exists()

    # 回放：客户端脚本给空的，一旦真去调用底层就会 IndexError——证明没打网络。
    replay_client = _client([])
    with replaying(path):
        first = await _two_calls(replay_client)
    with replaying(path):
        second = await _two_calls(replay_client)

    assert first == recorded
    assert second == recorded
    assert _inner_calls(replay_client) == []


@pytest.mark.asyncio
async def test_recorded_tape_keeps_kind_order_and_messages(tmp_path: Path) -> None:
    path = tmp_path / "tape.json"
    with recording(path, scenario="tests/fixtures/keeper_module.json"):
        await _two_calls(_client(['{"checks": []}', "正文"]))

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["scenario"] == "tests/fixtures/keeper_module.json"
    assert [e["kind"] for e in raw["entries"]] == ["adjudicate", "narrate"]
    # 录全文，人工审阅磁带时要看得懂上下文
    assert raw["entries"][0]["messages"][0]["content"] == "玩家：我翻找书桌"


@pytest.mark.asyncio
async def test_replay_reports_drift_but_does_not_raise(tmp_path: Path) -> None:
    """prompt 变了不该让磁带作废——如实记成漂移，交给人判断是不是预期内。"""
    path = tmp_path / "tape.json"
    with recording(path, scenario="tests/fixtures/keeper_module.json"):
        await _two_calls(_client(['{"checks": []}', "正文"]))

    with replaying(path) as session:
        got = await _client([]).chat.completions.create(
            tape_kind="adjudicate",
            model="m",
            messages=[{"role": "user", "content": "玩家：我翻找书桌（prompt 改过了）"}],
            response_format={"type": "json_object"},
        )

    assert got.choices[0].message.content == '{"checks": []}'
    assert len(session.drifts) == 1
    assert session.drifts[0].kind == "adjudicate"


@pytest.mark.asyncio
async def test_replay_raises_when_call_order_changed(tmp_path: Path) -> None:
    path = tmp_path / "tape.json"
    with recording(path, scenario="tests/fixtures/keeper_module.json"):
        await _two_calls(_client(['{"checks": []}', "正文"]))

    with replaying(path), pytest.raises(TapeMismatch):
        await _client([]).chat.completions.create(
            tape_kind="narrate", model="m", messages=[], temperature=0.8
        )


@pytest.mark.asyncio
async def test_replay_raises_when_code_calls_model_more_times(tmp_path: Path) -> None:
    path = tmp_path / "tape.json"
    with recording(path, scenario="tests/fixtures/keeper_module.json"):
        await _two_calls(_client(['{"checks": []}', "正文"]))

    with replaying(path):
        client = _client([])
        await _two_calls(client)
        with pytest.raises(TapeExhausted):
            await client.chat.completions.create(
                tape_kind="narrate", model="m", messages=[], temperature=0.8
            )


@pytest.mark.asyncio
async def test_no_tape_active_is_pure_passthrough() -> None:
    client = _client(["直接返回"])
    got = await client.chat.completions.create(tape_kind="narrate", model="m", messages=[])
    assert got.choices[0].message.content == "直接返回"
    # tape_kind 不能泄漏到真实请求里
    assert "tape_kind" not in _inner_calls(client)[0]


# ── 流式（exec/28）───────────────────────────────────────────


class _FakeDelta:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeStreamChunk:
    def __init__(self, content: str | None, finish_reason: str | None = None) -> None:
        self.choices = [_FakeStreamChoice(content, finish_reason)]


class _FakeStreamChoice:
    def __init__(self, content: str | None, finish_reason: str | None) -> None:
        self.delta = _FakeDelta(content)
        self.finish_reason = finish_reason


class _FakeStream:
    def __init__(self, pieces: list[str]) -> None:
        self._pieces = pieces

    def __aiter__(self):  # noqa: ANN204
        async def gen():  # noqa: ANN202
            for p in self._pieces:
                yield _FakeStreamChunk(p)
            yield _FakeStreamChunk(None, finish_reason="stop")

        return gen()


class _FakeStreamingCompletions(_FakeCompletions):
    """`create(stream=True)` 时返回一个假的流，否则退回普通响应。"""

    async def create(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        text = self._script.pop(0)
        if kwargs.get("stream"):
            return _FakeStream([text[i : i + 3] for i in range(0, len(text), 3)])
        return _FakeResponse(
            choices=[_FakeChoice(message=_FakeMessage(text), finish_reason="stop")]
        )


def _streaming_client(script: list[str]) -> TapedClient:
    inner = _FakeInner([])
    inner.chat.completions = _FakeStreamingCompletions(script)
    return TapedClient(inner)


@pytest.mark.asyncio
async def test_live_stream_yields_pieces_and_keeps_full_text() -> None:
    client = _streaming_client(["书桌抽屉里空空如也。"])
    call = client.chat.completions.stream(tape_kind="narrate", model="m", messages=[])

    pieces = [d async for d in call]

    assert len(pieces) > 1, "应该是分多次到达的，不是一次给完"
    assert "".join(pieces) == "书桌抽屉里空空如也。"
    assert call.text == "书桌抽屉里空空如也。"
    assert call.finish_reason == "stop"
    assert "tape_kind" not in _inner_calls(client)[0]


@pytest.mark.asyncio
async def test_stream_records_full_text_and_replays_without_network(tmp_path: Path) -> None:
    """🔴 磁带存的一直是完整响应，流式只是投递方式——**已有磁带不用重录**。"""
    path = tmp_path / "tape.json"
    client = _streaming_client(["灯还亮着，值班日志摊在桌上。"])

    with recording(path, scenario="tests/fixtures/keeper_module.json"):
        call = client.chat.completions.stream(tape_kind="narrate", model="m", messages=[])
        recorded = "".join([d async for d in call])

    # 回放客户端脚本给空的：一旦真去调底层就会 IndexError，证明没打网络
    replay_client = _streaming_client([])
    with replaying(path):
        replay_call = replay_client.chat.completions.stream(
            tape_kind="narrate", model="m", messages=[]
        )
        replayed = [d async for d in replay_call]

    assert "".join(replayed) == recorded
    assert replay_call.finish_reason == "stop"
    assert len(replayed) > 1, "回放也要分块，才能走到分段器的各条路径"
    assert _inner_calls(replay_client) == []


@pytest.mark.asyncio
async def test_stream_flag_does_not_change_request_digest(tmp_path: Path) -> None:
    """🔴 `stream=True` 不进 digest：非流式录的磁带，流式回放时不该报漂移。

    磁带记的是"给了什么上下文、模型答了什么"，投递方式不属于那份记录。
    这条一旦破了，改用流式就要把所有磁带重录一遍。
    """
    path = tmp_path / "tape.json"
    client = _streaming_client(["门厅里没有人。"])

    with recording(path, scenario="tests/fixtures/keeper_module.json"):
        await client.chat.completions.create(
            tape_kind="narrate", model="m", messages=[{"role": "user", "content": "写一段"}]
        )

    replay_client = _streaming_client([])
    with replaying(path) as session:
        call = replay_client.chat.completions.stream(
            tape_kind="narrate", model="m", messages=[{"role": "user", "content": "写一段"}]
        )
        assert "".join([d async for d in call]) == "门厅里没有人。"

    assert session.drifts == []


def test_committed_tapes_only_use_original_scenarios() -> None:
    """🔴 版权红线：进 git 的磁带只允许录原创迷你剧本。

    真实模组的 system prompt 常驻整份剧本正文，磁带落盘即等于把第三方版权
    正文写进仓库。真实对局的磁带一律留在 gitignored 的 `trpg-backend/tapes/`。
    """
    if not COMMITTED_TAPES_DIR.exists():
        # pytest.skip 被 @_with_exception 包过，ty 推不出它的真实签名
        pytest.skip("还没有已提交的磁带")  # ty: ignore[too-many-positional-arguments]
    for tape_path in COMMITTED_TAPES_DIR.glob("*.json"):
        tape = Tape.load(tape_path)
        assert tape.scenario in COMMITTABLE_SCENARIOS, (
            f"{tape_path.name} 录的是 {tape.scenario}，不在可提交白名单里"
        )
