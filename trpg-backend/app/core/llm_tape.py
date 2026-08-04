"""LLM 调用录制 / 回放（磁带）—— exec/14 的 P0 安全网。

## 为什么要它

keeper 的行为由「模型输出」和「代码逻辑」共同决定，而模型输出不可复现。
后果是：真人实测撞出来的每个 bug，修完就没有守门人——下次改 prose_discipline
的正则、改上下文组装、改 schema，都可能让它悄悄退化，而 pytest 全绿。

把 `(请求, 响应)` 录成磁带，就能在**断网**状态下重放同一批模型输出，断言
代码行为不变。这是 exec/14 里 P1（事实寻址）/ P2（主体与权限）那次大重构
的前提：重构完必须能证明「同样的模型输出 → 同样的行为」。

## 边界（别高估它）

只保证「给定模型输出，代码行为不变」，**不保证**「新 prompt 生成的内容更好」。
它是代码的回归网，不是叙事质量的评测器。质量评测是另一件事（要么真人实测，
要么批量质检脚本）。

## 为什么按序回放，而不是按请求哈希匹配

哈希键在 P1 之后会全部失效：事实寻址会改 `render_full` 的输出 → system prompt
变 → 哈希全部 miss → 磁带集体作废。按序回放能穿过 prompt 变化继续用，同时把
digest 差异作为「漂移」如实报出来，供人判断这次 prompt 变化是不是预期内的。

代价是磁带假设调用顺序稳定。因此回放时仍然校验 `kind`（adjudicate / narrate /
…）：顺序一旦对不上就**立刻报错**，而不是静默返回错位的响应。

⚠️ 单进程内并发对局 / 心跳插队会打乱顺序——录制真实对局时请只开一局、
关掉心跳（`KEEPER_HEARTBEAT_ENABLED=false`）。

## 🔴 版权

真实模组的磁带里含剧本正文（system prompt 常驻整份 `render_full`），
**一律落在 gitignored 的 `trpg-backend/tapes/`**。只有原创迷你剧本
（`tests/fixtures/keeper_module.json`）的磁带才允许进 git，放 `tests/tapes/`，
并由 `tests/test_llm_tape.py::test_committed_tapes_only_use_original_scenarios`
兜底——职责兜底在代码，不在提醒。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog

logger = structlog.get_logger(__name__)

TapeMode = Literal["off", "record", "replay"]

#: 允许进 git 的磁带场景 id。真实模组一律不在此列（版权红线）。
COMMITTABLE_SCENARIOS = frozenset({"tests/fixtures/keeper_module.json"})

_ENV_MODE = "LLM_TAPE_MODE"
_ENV_PATH = "LLM_TAPE_PATH"
_ENV_SCENARIO = "LLM_TAPE_SCENARIO"


@dataclass
class TapeEntry:
    """一次 LLM 往返。

    `messages` 录制时存全文（人工审阅磁带时要看得懂上下文），回放时不依赖它——
    回放只按顺序取 `response_text`。
    """

    index: int
    kind: str
    model: str
    request_digest: str
    response_text: str
    finish_reason: str | None = None
    messages: list[dict[str, Any]] | None = None


@dataclass
class Drift:
    """回放时发现「请求变了」——不报错，如实记下来供调用方判断。"""

    index: int
    kind: str
    recorded_digest: str
    actual_digest: str


@dataclass
class Tape:
    scenario: str
    entries: list[TapeEntry] = field(default_factory=list)

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"scenario": self.scenario, "entries": [asdict(e) for e in self.entries]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> Tape:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            scenario=raw["scenario"],
            entries=[TapeEntry(**e) for e in raw["entries"]],
        )


def request_digest(model: str, messages: list[Any], params: dict[str, Any]) -> str:
    """请求指纹。只用于漂移检测，不用于查找。"""
    payload = json.dumps(
        {"model": model, "messages": messages, "params": params},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class TapeSession:
    """一次录制或回放。进程内全局单例，由上下文管理器装卸。"""

    def __init__(self, mode: TapeMode, tape: Tape, path: Path | None = None) -> None:
        self.mode = mode
        self.tape = tape
        self.path = path
        self.cursor = 0
        self.drifts: list[Drift] = []

    def record(
        self,
        *,
        kind: str,
        model: str,
        messages: list[Any],
        params: dict[str, Any],
        response_text: str,
        finish_reason: str | None,
    ) -> None:
        self.tape.entries.append(
            TapeEntry(
                index=len(self.tape.entries),
                kind=kind,
                model=model,
                request_digest=request_digest(model, messages, params),
                response_text=response_text,
                finish_reason=finish_reason,
                messages=[dict(m) for m in messages],
            )
        )

    def next_entry(
        self, *, kind: str, model: str, messages: list[Any], params: dict[str, Any]
    ) -> TapeEntry:
        if self.cursor >= len(self.tape.entries):
            raise TapeExhausted(
                f"磁带只录了 {len(self.tape.entries)} 次调用，第 {self.cursor + 1} 次"
                f"（kind={kind}）没有对应录音——代码比录制时多调了一次模型。"
            )
        entry = self.tape.entries[self.cursor]
        self.cursor += 1
        if entry.kind != kind:
            raise TapeMismatch(
                f"第 {entry.index} 次调用录的是 kind={entry.kind}，实际是 kind={kind}"
                "——调用顺序变了，磁带对不上。"
            )
        actual = request_digest(model, messages, params)
        if actual != entry.request_digest:
            self.drifts.append(
                Drift(
                    index=entry.index,
                    kind=kind,
                    recorded_digest=entry.request_digest,
                    actual_digest=actual,
                )
            )
        return entry

    def flush(self) -> None:
        if self.mode == "record" and self.path is not None:
            self.tape.dump(self.path)


class TapeExhausted(RuntimeError):
    """回放时代码要的调用次数超过磁带录的次数。"""


class TapeMismatch(RuntimeError):
    """回放时调用顺序与磁带不符。"""


_active: TapeSession | None = None


def active_session() -> TapeSession | None:
    return _active


@contextmanager
def recording(path: str | Path, *, scenario: str) -> Iterator[TapeSession]:
    """录制：真实打网络，同时把每次往返落盘。"""
    global _active
    session = TapeSession("record", Tape(scenario=scenario), Path(path))
    previous, _active = _active, session
    try:
        yield session
    finally:
        session.flush()
        _active = previous


@contextmanager
def replaying(path: str | Path) -> Iterator[TapeSession]:
    """回放：完全不打网络，按序返回录好的响应。"""
    global _active
    session = TapeSession("replay", Tape.load(Path(path)))
    previous, _active = _active, session
    try:
        yield session
    finally:
        _active = previous


@dataclass
class _ReplayMessage:
    content: str | None


@dataclass
class _ReplayChoice:
    message: _ReplayMessage
    finish_reason: str | None


@dataclass
class _ReplayResponse:
    """只实现调用方真正读到的字段（`.choices[0].message.content` /
    `.finish_reason`），不复刻整个 openai 响应类型。

    🔴 `usage` 恒为 `None`：磁带**没有录 token 数**。回放时统计出来的调用成本
    因此会是 0——这是如实的"不知道"，不是 0 次调用。导入管线那三个脚本本来就
    写着 `if usage is not None`，照旧走得通。
    """

    choices: list[_ReplayChoice]
    usage: Any = None


def _split_call(kwargs: dict[str, Any]) -> tuple[str, list[Any], dict[str, Any]]:
    model = kwargs.get("model", "")
    messages = list(kwargs.get("messages", []))
    params = {k: v for k, v in kwargs.items() if k not in ("model", "messages")}
    return model, messages, params


def _replay_or_none(
    session: TapeSession | None,
    *,
    tape_kind: str,
    model: str,
    messages: list[Any],
    params: dict[str, Any],
) -> _ReplayResponse | None:
    """在放磁带就返回录好的那一条，否则返回 None（调用方去真打网络）。"""
    if session is None or session.mode != "replay":
        return None
    entry = session.next_entry(kind=tape_kind, model=model, messages=messages, params=params)
    return _ReplayResponse(
        choices=[
            _ReplayChoice(
                message=_ReplayMessage(content=entry.response_text),
                finish_reason=entry.finish_reason,
            )
        ]
    )


def _record_if_needed(
    session: TapeSession | None,
    response: Any,
    *,
    tape_kind: str,
    model: str,
    messages: list[Any],
    params: dict[str, Any],
) -> None:
    if session is None or session.mode != "record":
        return
    choice = response.choices[0]
    session.record(
        kind=tape_kind,
        model=model,
        messages=messages,
        params=params,
        response_text=choice.message.content or "",
        finish_reason=choice.finish_reason,
    )
    session.flush()


class _TapedCompletions:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def create(self, *, tape_kind: str, **kwargs: Any) -> Any:
        session = active_session()
        model, messages, params = _split_call(kwargs)

        replayed = _replay_or_none(
            session, tape_kind=tape_kind, model=model, messages=messages, params=params
        )
        if replayed is not None:
            return replayed

        response = await self._inner.create(**kwargs)
        _record_if_needed(
            session, response, tape_kind=tape_kind, model=model, messages=messages, params=params
        )
        return response

    def stream(self, *, tape_kind: str, **kwargs: Any) -> StreamCall:
        """流式版 `create`。返回 `StreamCall`，见它的 docstring。

        🔴 `stream=True` **不进 params**，所以 `request_digest` 与非流式一致，
        已有磁带可以直接回放、不用重录。磁带记的是"给了什么上下文、模型答了
        什么"，投递方式不属于那份记录（`exec/28` 第 6 节）。
        """
        session = active_session()
        model = kwargs.get("model", "")
        messages = list(kwargs.get("messages", []))
        params = {k: v for k, v in kwargs.items() if k not in ("model", "messages")}

        if session is not None and session.mode == "replay":
            entry = session.next_entry(
                kind=tape_kind, model=model, messages=messages, params=params
            )
            return _ReplayStreamCall(entry.response_text, entry.finish_reason)

        return _LiveStreamCall(
            self._inner,
            kwargs,
            session=session,
            tape_kind=tape_kind,
            model=model,
            messages=messages,
            params=params,
        )


class StreamCall:
    """一次流式补全。

    用法：
        call = client.chat.completions.stream(tape_kind="narrate", ...)
        async for delta in call:
            ...
        call.text            # 完整原文（落库/记账用它，不是拼 delta）
        call.finish_reason   # 迭代结束后才可读

    🔴 **回放时按块切完整文本模拟流式，不重录磁带**（`exec/28` 第 6 节）。
    磁带存的一直是完整响应，流式只是投递方式的变化。
    """

    #: 回放时模拟的块大小。刻意切得比真实 chunk 碎，让分段器的各条路径都被走到。
    REPLAY_CHUNK = 8

    def __init__(self) -> None:
        self._text = ""
        self._finish_reason: str | None = None
        self._done = False

    @property
    def text(self) -> str:
        return self._text

    @property
    def finish_reason(self) -> str | None:
        return self._finish_reason

    def __aiter__(self) -> AsyncIterator[str]:
        raise NotImplementedError


class _ReplayStreamCall(StreamCall):
    def __init__(self, full_text: str, finish_reason: str | None) -> None:
        super().__init__()
        self._full = full_text
        self._pending_reason = finish_reason

    async def __aiter__(self) -> AsyncIterator[str]:
        for i in range(0, len(self._full), self.REPLAY_CHUNK):
            piece = self._full[i : i + self.REPLAY_CHUNK]
            self._text += piece
            yield piece
        self._finish_reason = self._pending_reason
        self._done = True


class _LiveStreamCall(StreamCall):
    def __init__(
        self,
        inner: Any,
        kwargs: dict[str, Any],
        *,
        session: TapeSession | None,
        tape_kind: str,
        model: str,
        messages: list[Any],
        params: dict[str, Any],
    ) -> None:
        super().__init__()
        self._inner = inner
        self._kwargs = kwargs
        self._session = session
        self._tape_kind = tape_kind
        self._model = model
        self._messages = messages
        self._params = params

    async def __aiter__(self) -> AsyncIterator[str]:
        stream = await self._inner.create(**self._kwargs, stream=True)
        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = getattr(choice.delta, "content", None)
            if delta:
                self._text += delta
                yield delta
            if choice.finish_reason:
                self._finish_reason = choice.finish_reason
        self._done = True
        if self._session is not None and self._session.mode == "record":
            self._session.record(
                kind=self._tape_kind,
                model=self._model,
                messages=self._messages,
                params=self._params,
                response_text=self._text,
                finish_reason=self._finish_reason,
            )
            self._session.flush()


class _TapedChat:
    def __init__(self, inner: Any) -> None:
        self.completions = _TapedCompletions(inner.chat.completions)


class TapedClient:
    """包在 `AsyncOpenAI` 外面的一层。没有磁带在录/在放时纯透传。

    调用方式与原客户端一致，只多一个必填的 `tape_kind`（adjudicate /
    narrate / …）——回放时靠它校验调用顺序没变。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.chat = _TapedChat(inner)


def build_llm_client(*, api_key: str, base_url: str, timeout: float) -> TapedClient:
    from openai import AsyncOpenAI

    return TapedClient(AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout))


# ── 同步那一半：模组导入管线（`exec/29`）────────────────────────────


class _TapedSyncCompletions:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def create(self, *, tape_kind: str, **kwargs: Any) -> Any:
        session = active_session()
        model, messages, params = _split_call(kwargs)

        replayed = _replay_or_none(
            session, tape_kind=tape_kind, model=model, messages=messages, params=params
        )
        if replayed is not None:
            return replayed

        response = self._inner.create(**kwargs)
        _record_if_needed(
            session, response, tape_kind=tape_kind, model=model, messages=messages, params=params
        )
        return response


class _TapedSyncChat:
    def __init__(self, inner: Any) -> None:
        self.completions = _TapedSyncCompletions(inner.chat.completions)


class TapedSyncClient:
    """`TapedClient` 的同步兄弟。

    ## 🔴 为什么需要它

    模组导入的转换链（`scripts/module_probe/`）是**同步**的，而且由
    `asyncio.to_thread` 在工作线程里跑（管线要 5–26 分钟，在事件循环里 await
    它等于整个后端冻住）。异步版的 `create` 在那里用不了，而"把三个脚本改成
    async"波及它们的 CLI 入口，代价远大于在这里加一条同步路径。

    录制/回放的记账逻辑与异步版**共用**（`_replay_or_none` / `_record_if_needed`），
    只有"怎么调 inner"不同——两份各写一遍迟早会漂。

    ## 🔴 `_active` 是模块级全局，不是 contextvar

    所以工作线程看得见主线程开的磁带，这条路才通。代价是**同一进程里并发录制
    会把顺序录乱**——录制是开发者显式动作（`LLM_TAPE_MODE=record`），录的时候
    只跑一条导入即可，跟"录真实对局要只开一局、关心跳"是同一条纪律。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.chat = _TapedSyncChat(inner)


def build_sync_llm_client(*, api_key: str, base_url: str, timeout: float) -> TapedSyncClient:
    from openai import OpenAI

    return TapedSyncClient(OpenAI(api_key=api_key, base_url=base_url, timeout=timeout))


def activate_from_env() -> TapeSession | None:
    """按环境变量启动录制——用于「起后端 + 真人玩一局」录真实对局。

        LLM_TAPE_MODE=record LLM_TAPE_PATH=tapes/xxx.json \\
        LLM_TAPE_SCENARIO=模组资料/追书人.structured.json \\
        KEEPER_HEARTBEAT_ENABLED=false .venv/bin/uvicorn app.main:app ...

    进程退出时才落盘不可靠（uvicorn 常被 Ctrl-C 打断），所以录制模式下每次
    往返都会立即重写整份磁带——磁带很小，代价可以忽略。
    """
    global _active
    mode = os.environ.get(_ENV_MODE)
    if mode != "record":
        return None
    path = os.environ.get(_ENV_PATH)
    if not path:
        raise ValueError(f"{_ENV_MODE}=record 时必须同时给 {_ENV_PATH}")
    session = TapeSession(
        "record", Tape(scenario=os.environ.get(_ENV_SCENARIO, "unknown")), Path(path)
    )
    _active = session
    logger.info("llm_tape_recording_started", path=path, scenario=session.tape.scenario)
    return session
