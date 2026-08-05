"""并行化 + 磁带按组回放（`exec/30` 步骤 1）。

## 这一条要守的是什么

并行本身不难，难的是**它把磁带的前提拆了**：磁带按序回放，而并行之后完成
顺序不确定。所以这两件事必须一起做，也必须一起验——

1. `run_parallel` **按输入顺序返回**（下游的合并、覆盖率统计、`zip(strict=True)`
   全靠这个；一旦退化成完成顺序，产物会随机变而不会有任何东西变红）
2. 带 `tape_key` 的调用**按 key 回放，与调用顺序无关**
3. 按序条目与按 key 条目**互不干扰**（keyed 的不能把 cursor 顶偏）
4. `tape_key_for` 给同一个 label 的第 N 次使用不同的键（自修会重跑阶段 2）

## 为什么不去测"真的变快了"

墙钟时间在 CI 上不稳，而且真正的提速要真打网络才量得到。这里守的是
**并行之后行为不变**；速度那一维在真机上量（`exec/30` §4.4 的表）。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.core.llm_tape import TapeExhausted, TapeMismatch, recording, replaying
from scripts.module_probe.parallel import run_parallel

# ── run_parallel ─────────────────────────────────────


def test_results_follow_input_order_not_completion_order() -> None:
    """🔴 后面几处 `zip(..., strict=True)` 全指望这个。

    故意让第一个任务最慢：如果实现改成"谁先完成谁先进结果"，这条立刻红。
    """

    def job(i: int, delay: float):
        def run() -> int:
            time.sleep(delay)
            return i

        return run

    jobs = [job(0, 0.05), job(1, 0.0), job(2, 0.0)]
    assert run_parallel(jobs, concurrency=3) == [0, 1, 2]


def test_actually_runs_concurrently() -> None:
    """没有这条，把 run_parallel 悄悄改成串行 for 循环也不会有东西变红。"""
    started = threading.Barrier(4, timeout=5)

    def job() -> str:
        started.wait()  # 四个任务不同时在跑就会超时
        return "ok"

    assert run_parallel([job] * 4, concurrency=4) == ["ok"] * 4


def test_concurrency_one_runs_inline() -> None:
    order: list[int] = []

    def job(i: int):
        def run() -> int:
            order.append(i)
            return i

        return run

    assert run_parallel([job(0), job(1), job(2)], concurrency=1) == [0, 1, 2]
    assert order == [0, 1, 2]


def test_first_failure_wins_regardless_of_timing() -> None:
    """两个任务都炸时，报靠前的那个——否则同一份输入两次跑出不同的报错。"""

    def slow_boom() -> int:
        time.sleep(0.05)
        raise ValueError("靠前的")

    def fast_boom() -> int:
        raise ValueError("靠后的")

    with pytest.raises(ValueError, match="靠前的"):
        run_parallel([slow_boom, fast_boom], concurrency=2)


def test_empty_is_not_an_error() -> None:
    assert run_parallel([]) == []


# ── 磁带：按 key 的组 ─────────────────────────────────


class _Resp:
    def __init__(self, content: str) -> None:
        self.choices = [
            type(
                "_C",
                (),
                {"message": type("_M", (), {"content": content})(), "finish_reason": "stop"},
            )()
        ]
        self.usage = None


class _Completions:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs) -> _Resp:
        self.calls += 1
        # 回声：把 user 消息原样吐回来，好断言"哪条录音配给了哪次调用"
        return _Resp(kwargs["messages"][0]["content"])


def _client():
    from app.core.llm_tape import TapedSyncClient

    inner = type("_I", (), {"chat": type("_C", (), {"completions": _Completions()})()})()
    return TapedSyncClient(inner), inner.chat.completions


def _call(client, *, kind: str, key: str | None, body: str):
    return (
        client.chat.completions.create(
            tape_kind=kind,
            tape_key=key,
            model="m",
            messages=[{"role": "user", "content": body}],
        )
        .choices[0]
        .message.content
    )


def test_keyed_entries_replay_out_of_order(tmp_path: Path) -> None:
    """录制顺序 a→b→c，回放顺序 c→a→b，每条仍拿到自己的录音。

    这正是并行组要的语义：完成顺序不确定，但每次调用算得出自己的 key。
    """
    tape = tmp_path / "t.json"
    client, live = _client()
    with recording(tape, scenario="tests/fixtures/keeper_module.json"):
        for name in ("a", "b", "c"):
            _call(client, kind="module_probe", key=f"batch:{name}", body=name)
    assert live.calls == 3

    replay_client, replay_live = _client()
    with replaying(tape):
        got = [
            _call(replay_client, kind="module_probe", key=f"batch:{n}", body="不该被用到")
            for n in ("c", "a", "b")
        ]

    assert got == ["c", "a", "b"]
    assert replay_live.calls == 0


def test_keyed_and_sequential_do_not_disturb_each_other(tmp_path: Path) -> None:
    """🔴 keyed 条目不能顶偏 cursor。

    真实形状就是这样：并行的关系发现夹在串行的阶段 1 / 阶段 3 中间。
    """
    tape = tmp_path / "t.json"
    client, _ = _client()
    with recording(tape, scenario="tests/fixtures/keeper_module.json"):
        _call(client, kind="module_assemble", key=None, body="stage1")
        _call(client, kind="module_relations", key="batch:0", body="b0")
        _call(client, kind="module_relations", key="batch:1", body="b1")
        _call(client, kind="module_assemble", key=None, body="stage3")

    replay_client, _ = _client()
    with replaying(tape):
        first = _call(replay_client, kind="module_assemble", key=None, body="x")
        # 并行组这次反着完成
        b1 = _call(replay_client, kind="module_relations", key="batch:1", body="x")
        b0 = _call(replay_client, kind="module_relations", key="batch:0", body="x")
        last = _call(replay_client, kind="module_assemble", key=None, body="x")

    assert [first, b0, b1, last] == ["stage1", "b0", "b1", "stage3"]


def test_missing_key_fails_loudly(tmp_path: Path) -> None:
    """并行组的成员变了（批次划分不同）要当场炸，不能静默串到别的录音上。"""
    tape = tmp_path / "t.json"
    client, _ = _client()
    with recording(tape, scenario="tests/fixtures/keeper_module.json"):
        _call(client, kind="module_relations", key="batch:0", body="b0")

    with replaying(tape), pytest.raises(TapeExhausted):
        _call(client, kind="module_relations", key="batch:9", body="x")


def test_duplicate_keys_on_a_tape_are_rejected_at_load(tmp_path: Path) -> None:
    """手工拼出来的坏磁带要在 load 时炸，而不是回放到一半取错一条。"""
    import json

    tape = tmp_path / "t.json"
    entry = {
        "index": 0,
        "kind": "module_probe",
        "model": "m",
        "request_digest": "x",
        "response_text": "a",
        "key": "dup",
    }
    tape.write_text(
        json.dumps({"scenario": "s", "entries": [entry, {**entry, "index": 1}]}),
        encoding="utf-8",
    )
    with pytest.raises(TapeMismatch, match="dup"), replaying(tape):
        pass


def test_retry_overwrites_instead_of_crashing_the_recording(tmp_path: Path) -> None:
    """🔴 同一个 key 录第二次是**重试**，不是撞车。

    响应回来了但 JSON 解析失败，业务侧会重发。为此中断录制 = 白烧一次钱。
    """
    tape = tmp_path / "t.json"
    client, _ = _client()
    with recording(tape, scenario="tests/fixtures/keeper_module.json"):
        _call(client, kind="module_probe", key="k", body="第一次")
        _call(client, kind="module_probe", key="k", body="第二次")

    replay_client, _ = _client()
    with replaying(tape):
        assert _call(replay_client, kind="module_probe", key="k", body="x") == "第二次"


# ── tape_key_for ─────────────────────────────────────


def test_repeated_label_gets_distinct_keys() -> None:
    """自修会回灌阶段 1 再重跑阶段 2——同一个 label 会出现好几遍。

    没有这个序号，第二轮会覆盖第一轮的录音，而回放时第二轮拿到第一轮的响应。
    """
    from scripts.module_probe.assemble import CallStats, tape_key_for

    stats = CallStats()
    keys = [tape_key_for(stats, "stage2.node:hall") for _ in range(3)]
    assert keys == ["stage2.node:hall", "stage2.node:hall#2", "stage2.node:hall#3"]
    assert tape_key_for(stats, "stage2.node:study") == "stage2.node:study"
