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

**一处已知并发是内建支持的**：模组导入把彼此独立的调用并行了（`exec/30` 步骤 1），
那些调用带一个稳定子键、按 key 回放而不按序，见 `TapeSession` 的 docstring。

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
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog

from app.core.llm_usage import log_call_usage

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

    `key` 是**并发调用**的稳定子键（见 `TapeSession` 的 docstring）。为 None 的
    条目走原来的按序回放；有 key 的按 key 查找，不参与顺序。
    """

    index: int
    kind: str
    model: str
    request_digest: str
    response_text: str
    finish_reason: str | None = None
    messages: list[dict[str, Any]] | None = None
    key: str | None = None


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
    """一次录制或回放。进程内全局单例，由上下文管理器装卸。

    ## 🔴 两种条目：按序的，和按 key 的

    按序回放的前提是调用顺序稳定。**并发一上，完成顺序就不确定了**——
    模组导入的关系发现是 8 批彼此独立的调用，串行跑掉整条链 70% 的墙钟时间
    （`exec/30` §4.4），而一旦 `gather` 起来，按序磁带当场炸。

    修法**不是**「录制时强制串行」——那样磁带跑的路径跟生产路径不同构，
    守不住真正在跑的那条。修法是给并发组里的每次调用一个**稳定子键**
    （`relations:pass1:batch3` 这种，由业务侧算得出、跑多少次都一样），
    磁带于是从「一条流水线」变成「若干有序的组」：

    - `key=None` 的调用：老规矩，按 `cursor` 顺序取，顺序变了立刻报错
    - 有 `key` 的调用：按 key 查，**不动 cursor**，所以并发组内乱序无所谓

    两者互不干扰：keyed 条目整体不参与 sequential 的计数。
    """

    def __init__(self, mode: TapeMode, tape: Tape, path: Path | None = None) -> None:
        self.mode = mode
        self.tape = tape
        self.path = path
        self.cursor = 0
        self.drifts: list[Drift] = []
        # 🔴 并发组是真的多线程在跑（导入管线用 ThreadPoolExecutor），录制要往
        # 同一个 list 追加、往同一个文件整份重写，两件事都得串起来。
        self._lock = threading.Lock()
        self._sequential = [e for e in tape.entries if e.key is None]
        self._by_key: dict[str, TapeEntry] = {}
        for entry in tape.entries:
            if entry.key is None:
                continue
            if entry.key in self._by_key:
                # 🔴 key 撞车 = 它不是稳定唯一的，回放会静默取错一条。宁可现在炸。
                raise TapeMismatch(f"磁带里有两条 key={entry.key!r} 的录音——子键必须唯一。")
            self._by_key[entry.key] = entry
        #: 录制时用来当场发现 key 撞车（录完再发现就白花钱了）。
        self._recorded_keys: set[str] = set(self._by_key)

    def record(
        self,
        *,
        kind: str,
        model: str,
        messages: list[Any],
        params: dict[str, Any],
        response_text: str,
        finish_reason: str | None,
        key: str | None = None,
    ) -> None:
        digest = request_digest(model, messages, params)
        with self._lock:
            entry = TapeEntry(
                index=len(self.tape.entries),
                kind=kind,
                model=model,
                request_digest=digest,
                response_text=response_text,
                finish_reason=finish_reason,
                messages=[dict(m) for m in messages],
                key=key,
            )
            if key is not None and key in self._recorded_keys:
                # 🔴 覆盖，不报错。同一个 key 录第二次的正常来路是**重试**——
                # 响应回来了但 JSON 解析失败，业务侧重发一次。为此中断录制等于
                # 白烧一次钱。真正的子键撞车（两个不同调用共用一个 key）会表现为
                # 磁带条数少于调用次数，由跑完打印的那个数字暴露。
                logger.warning("llm_tape_key_overwritten", key=key, kind=kind)
                self._replace_locked(key, entry)
            else:
                if key is not None:
                    self._recorded_keys.add(key)
                self.tape.entries.append(entry)
            self._dump_locked()

    def _replace_locked(self, key: str, entry: TapeEntry) -> None:
        for i, old in enumerate(self.tape.entries):
            if old.key == key:
                entry.index = old.index
                self.tape.entries[i] = entry
                return

    def next_entry(
        self,
        *,
        kind: str,
        model: str,
        messages: list[Any],
        params: dict[str, Any],
        key: str | None = None,
    ) -> TapeEntry:
        entry = self._lookup(kind=kind, key=key)
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

    def _lookup(self, *, kind: str, key: str | None) -> TapeEntry:
        if key is not None:
            entry = self._by_key.get(key)
            if entry is None:
                raise TapeExhausted(
                    f"磁带里没有 key={key!r}（kind={kind}）的录音——"
                    "并发组的成员变了（批次划分/实体集合跟录制时不同）。"
                )
            return entry
        with self._lock:
            if self.cursor >= len(self._sequential):
                raise TapeExhausted(
                    f"磁带只录了 {len(self._sequential)} 次按序调用，第 {self.cursor + 1} 次"
                    f"（kind={kind}）没有对应录音——代码比录制时多调了一次模型。"
                )
            entry = self._sequential[self.cursor]
            self.cursor += 1
        return entry

    def flush(self) -> None:
        with self._lock:
            self._dump_locked()

    def _dump_locked(self) -> None:
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
    tape_key: str | None,
    model: str,
    messages: list[Any],
    params: dict[str, Any],
) -> _ReplayResponse | None:
    """在放磁带就返回录好的那一条，否则返回 None（调用方去真打网络）。"""
    if session is None or session.mode != "replay":
        return None
    entry = session.next_entry(
        kind=tape_kind, key=tape_key, model=model, messages=messages, params=params
    )
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
    tape_key: str | None,
    model: str,
    messages: list[Any],
    params: dict[str, Any],
) -> None:
    if session is None or session.mode != "record":
        return
    choice = response.choices[0]
    session.record(
        kind=tape_kind,
        key=tape_key,
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

    async def create(self, *, tape_kind: str, tape_key: str | None = None, **kwargs: Any) -> Any:
        session = active_session()
        model, messages, params = _split_call(kwargs)

        replayed = _replay_or_none(
            session,
            tape_kind=tape_kind,
            tape_key=tape_key,
            model=model,
            messages=messages,
            params=params,
        )
        if replayed is not None:
            return replayed

        response = await self._inner.create(**kwargs)
        # 观测：这次调用的 token 账与前缀缓存命中情况。只在真实调用之后记——
        # 回放出来的假响应没有 usage，记了也是假的。
        log_call_usage(kind=tape_kind, response=response)
        _record_if_needed(
            session,
            response,
            tape_kind=tape_kind,
            tape_key=tape_key,
            model=model,
            messages=messages,
            params=params,
        )
        return response

    def stream(self, *, tape_kind: str, tape_key: str | None = None, **kwargs: Any) -> StreamCall:
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
                kind=tape_kind, key=tape_key, model=model, messages=messages, params=params
            )
            return _ReplayStreamCall(entry.response_text, entry.finish_reason)

        return _LiveStreamCall(
            self._inner,
            kwargs,
            session=session,
            tape_kind=tape_kind,
            tape_key=tape_key,
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
        tape_key: str | None = None,
        model: str,
        messages: list[Any],
        params: dict[str, Any],
    ) -> None:
        super().__init__()
        self._inner = inner
        self._kwargs = kwargs
        self._session = session
        self._tape_kind = tape_kind
        self._tape_key = tape_key
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
                key=self._tape_key,
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

    def create(self, *, tape_kind: str, tape_key: str | None = None, **kwargs: Any) -> Any:
        session = active_session()
        model, messages, params = _split_call(kwargs)

        replayed = _replay_or_none(
            session,
            tape_kind=tape_kind,
            tape_key=tape_key,
            model=model,
            messages=messages,
            params=params,
        )
        if replayed is not None:
            return replayed

        response = self._inner.create(**kwargs)
        _record_if_needed(
            session,
            response,
            tape_kind=tape_kind,
            tape_key=tape_key,
            model=model,
            messages=messages,
            params=params,
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

    所以工作线程看得见主线程开的磁带，这条路才通——而且导入内部自己的并行
    （`exec/30` 步骤 1）也是线程，同样看得见。**组内乱序由稳定子键解决**
    （`TapeSession`），会话本身的记账加了锁。

    仍然要守的纪律：**录制时只跑一条导入**。两条导入的按序段会互相插队，
    那是子键管不到的，跟"录真实对局要只开一局、关心跳"是同一条。
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
