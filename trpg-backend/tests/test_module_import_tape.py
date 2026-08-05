"""导入管线接上磁带（`exec/29` 收尾这笔欠账）。

## 为什么补这条

转换链的三个脚本原本各自 `OpenAI(api_key=…)`，于是**整条导入过程录不了也放
不了**——改一次组装 prompt，唯一的验证手段是花 ¥0.35 跑 5–26 分钟。而
`exec/29` 这一轮真改了 prompt（阶段 1 的规则 C、自修器那条 structure），
当时只有单元测试守着，端到端没验过。

## 两组用例，边界不一样

- **同步路径本身**（本文件大部分）：不需要磁带，进 CI。守的是"录/放的记账
  逻辑跟异步版共用、没有各写一遍"。
- **端到端回归**（最后一条）：需要一条真磁带，而**真实模组的磁带含正文、只能
  落 gitignored 的 `tapes/`**，所以它进不了 CI，没磁带时 skip。这是有意的取舍
  （见 `pipeline.py` 模块文档），不是没写完。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from app.core.llm_tape import (
    TapedClient,
    TapedSyncClient,
    recording,
    replaying,
)


class _FakeUsage:
    prompt_tokens = 11
    completion_tokens = 22
    total_tokens = 33


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)
        self.finish_reason = "stop"


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]
        self.usage = _FakeUsage()


class _FakeCompletions:
    """同步的假 completions。记下被调用几次，用来断言回放**没有打网络**。"""

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.calls = 0

    def create(self, **_kwargs) -> _FakeResponse:
        reply = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return _FakeResponse(reply)


class _FakeInner:
    def __init__(self, replies: list[str]) -> None:
        self.chat = type("_Chat", (), {"completions": _FakeCompletions(replies)})()


def _client(replies: list[str]) -> tuple[TapedSyncClient, _FakeCompletions]:
    inner = _FakeInner(replies)
    return TapedSyncClient(inner), inner.chat.completions


# ── 录 / 放 ───────────────────────────────────────────


def test_record_then_replay_returns_the_same_text(tmp_path: Path) -> None:
    tape = tmp_path / "t.json"
    client, live = _client(['{"items": []}', '{"relations": []}'])

    with recording(tape, scenario="tests/fixtures/keeper_module.json"):
        first = client.chat.completions.create(tape_kind="module_probe", model="m", messages=[])
        second = client.chat.completions.create(
            tape_kind="module_relations", model="m", messages=[]
        )

    assert live.calls == 2
    recorded = [first.choices[0].message.content, second.choices[0].message.content]

    replay_client, replay_live = _client(["不该被用到"])
    with replaying(tape):
        got = [
            replay_client.chat.completions.create(tape_kind="module_probe", model="m", messages=[])
            .choices[0]
            .message.content,
            replay_client.chat.completions.create(
                tape_kind="module_relations", model="m", messages=[]
            )
            .choices[0]
            .message.content,
        ]

    assert got == recorded
    assert replay_live.calls == 0, "回放绝不能打网络——那是这套装置存在的全部意义"


def test_replay_reports_usage_as_none_not_zero(tmp_path: Path) -> None:
    """🔴 磁带没录 token 数，回放时 `usage` 是 `None`。

    三个脚本都写着 `if usage is not None` 才累加，所以统计会是 0——那是如实的
    "不知道"。**不能伪造一个 0 的 usage 对象**：那样成本报表会看起来像真的。
    """
    tape = tmp_path / "t.json"
    client, _ = _client(["x"])
    with recording(tape, scenario="tests/fixtures/keeper_module.json"):
        live = client.chat.completions.create(tape_kind="module_probe", model="m", messages=[])
    assert live.usage is not None

    with replaying(tape):
        replayed = client.chat.completions.create(tape_kind="module_probe", model="m", messages=[])

    assert replayed.usage is None


def test_wrong_order_fails_loudly(tmp_path: Path) -> None:
    """顺序对不上要当场炸，不能静默返回错位的响应。"""
    tape = tmp_path / "t.json"
    client, _ = _client(["a"])
    with recording(tape, scenario="tests/fixtures/keeper_module.json"):
        client.chat.completions.create(tape_kind="module_probe", model="m", messages=[])

    with replaying(tape), pytest.raises(Exception):  # noqa: B017 — 具体类型见 llm_tape
        client.chat.completions.create(tape_kind="module_assemble", model="m", messages=[])


def test_passthrough_when_no_tape_is_active() -> None:
    """没在录也没在放时纯透传——CLI 平时跑的就是这条路。"""
    client, live = _client(["hi"])

    got = client.chat.completions.create(tape_kind="module_probe", model="m", messages=[])

    assert got.choices[0].message.content == "hi"
    assert live.calls == 1


# ── 🔴 两份记账逻辑必须共用 ──────────────────────────


def test_sync_and_async_share_the_bookkeeping() -> None:
    """异步版与同步版只能差在"怎么调 inner"，记账走同一对函数。

    各写一遍必然漂：加一个字段只改一边，症状是"录的时候好好的，回放就不对"。
    """
    from app.core.llm_tape import _TapedCompletions, _TapedSyncCompletions

    for cls in (_TapedCompletions, _TapedSyncCompletions):
        body = inspect.getsource(cls)
        assert "_replay_or_none" in body, f"{cls.__name__} 没走共用的回放分支"
        assert "_record_if_needed" in body, f"{cls.__name__} 没走共用的录制分支"


def test_sync_client_is_not_the_async_one() -> None:
    """两个类各管一边，别把 `TapedClient` 塞进同步管线（`await` 会炸在线程里）。"""
    assert TapedSyncClient is not TapedClient
    assert not inspect.iscoroutinefunction(TapedSyncClient(_FakeInner([])).chat.completions.create)


# ── 🔴 CLI 得自己开磁带 ──────────────────────────────


def test_cli_activates_the_tape_itself() -> None:
    """🔴 `LLM_TAPE_MODE=record` 在命令行下必须真的开始录。

    实测踩过：`activate_from_env()` 原本只在 `app/main.py` 的 lifespan 里被调用
    ——**服务端那条路有人开，命令行这条没有**。于是设了环境变量跑一趟，花掉
    ¥0.35 / 3 分钟，磁带一条都没录上，而且全程没有任何提示：空磁带和"没开录制"
    看起来一模一样。

    同族于「探测器不是闸门，零命中 ≠ 没问题」——这次是「录了 0 条 ≠ 录制在工作」。
    """
    from scripts.module_probe import pipeline

    src = inspect.getsource(pipeline.main)

    assert "activate_from_env()" in src, "CLI 没开磁带，录制会是静默空操作"
    assert "session.tape.entries" in src, "跑完要报条数——不报就没人会发现录了 0 条"


# ── 端到端回归：要磁带，进不了 CI ───────────────────


_BACKEND = Path(__file__).resolve().parents[1]
_TAPE = _BACKEND / "tapes" / "module-import.json"
_EXPECTED = _BACKEND / "tapes" / "module-import.expected.json"
#: 录制时用的源文件。`模组资料/` 是 gitignored 的第三方正文。
_SOURCE = _BACKEND.parent / "模组资料" / "林中屋.pdf"


def _skip_unless_taped() -> None:
    for path, what in ((_TAPE, "磁带"), (_EXPECTED, "基准产物"), (_SOURCE, "源文件")):
        if not path.exists():
            msg = f"缺少{what} {path.name}，跳过端到端回归（录一条见 pipeline.py 文档）"
            pytest.skip(msg)  # ty: ignore[too-many-positional-arguments]


def test_tape_only_contains_import_pipeline_calls() -> None:
    """磁带里不该混进导入管线之外的调用——那说明录制时有别的东西在跑。"""
    _skip_unless_taped()
    from app.core.llm_tape import Tape

    kinds = {e.kind for e in Tape.load(_TAPE).entries}

    assert kinds <= {"module_probe", "module_relations", "module_assemble"}
    assert "module_assemble" in kinds, "磁带没录到组装那一步，说明当时没跑完"


def test_import_pipeline_replays_to_the_same_structured(tmp_path: Path) -> None:
    """🔴 **断网重放整条转换链，产出必须与录制时逐字节一致。**

    这才是这套装置的兑现点。只查磁带形状是不够的——那只证明"文件长得像磁带"，
    不证明"回放能替代那 ¥0.35"。

    它守的是：改组装代码 / 改 prompt 组装 / 重构脚本之后，**给定同样的模型输出，
    产物没变**。变了就是代码行为变了，跟模型无关。

    没磁带就 skip：真实模组的磁带含正文（prompt 是整份原文、响应是 structured
    产物），只能落 gitignored 的 `tapes/`，进不了 CI。这是取舍不是没写完。
    """
    _skip_unless_taped()

    from app.core.llm_tape import replaying
    from scripts.module_probe import pipeline

    out = tmp_path / "structured.json"
    with replaying(_TAPE):
        result = pipeline.convert(_SOURCE, work_dir=tmp_path / "work", out_structured=out)

    # 🔴 录制那次的结局本身也是产物的一部分：那一跑是**被拒绝**的
    # （4 条，skill + thin_slot）。回放要重现的是「同样的模型输出 → 同样的
    # 判决」，不是"这份模组是好的"。
    #
    # 🔴 这几个数字会随重录而变，那是磁带的性质不是 bug：录到哪条路径取决于
    # 录制那一刻模型的输出。重录后照着新结果改这里，别反过来去"修"代码。
    assert result.hard_failures == 4
    assert "skill" in result.failure_reason

    assert json.loads(out.read_text(encoding="utf-8")) == json.loads(
        _EXPECTED.read_text(encoding="utf-8")
    ), "回放产出的 structured 与录制时不一致——代码行为变了"
