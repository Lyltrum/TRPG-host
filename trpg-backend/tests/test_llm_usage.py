"""每次调用的 token 账与前缀缓存命中（`app/core/llm_usage.py`）。

只观测，不改行为。所以用例盯两件事：**算得对**，以及**字段缺了不许炸**——
那几个 cache 字段是 DeepSeek 特有的，换 provider、SDK 升级、磁带回放的假响应
都可能没有它们，而观测坏掉不能连累一局游戏。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.core.llm_usage import log_call_usage


def _capture(monkeypatch) -> list[dict]:
    captured: list[dict] = []

    class _Spy:
        def info(self, event: str, **kwargs: object) -> None:
            captured.append({"event": event, **kwargs})

    monkeypatch.setattr("app.core.llm_usage.logger", _Spy())
    return captured


def _response(**usage):
    return SimpleNamespace(usage=SimpleNamespace(**usage))


def test_命中率算好了再记(monkeypatch) -> None:
    """别让读日志的人自己去除——除错了不会有人发现。"""
    captured = _capture(monkeypatch)
    log_call_usage(
        kind="adjudicate",
        response=_response(
            prompt_tokens=30000,
            completion_tokens=500,
            prompt_cache_hit_tokens=27000,
            prompt_cache_miss_tokens=3000,
        ),
    )
    assert captured[0]["cache_hit_ratio"] == 0.9
    assert captured[0]["kind"] == "adjudicate"
    assert captured[0]["prompt_tokens"] == 30000


def test_没有缓存字段时照常记别的(monkeypatch) -> None:
    """🔴 换 provider / SDK 升级都可能没有那两个字段——不许炸，也不许因此
    把 prompt_tokens 一起丢掉。"""
    captured = _capture(monkeypatch)
    log_call_usage(kind="narrate", response=_response(prompt_tokens=100, completion_tokens=20))
    assert captured[0]["prompt_tokens"] == 100
    assert "cache_hit_tokens" not in captured[0]
    assert "cache_hit_ratio" not in captured[0]


def test_完全没有usage就什么都不记(monkeypatch) -> None:
    """回放出来的假响应没有 usage。记一行全是空的日志只会污染观测。"""
    captured = _capture(monkeypatch)
    log_call_usage(kind="adjudicate", response=SimpleNamespace())
    log_call_usage(kind="adjudicate", response=SimpleNamespace(usage=None))
    log_call_usage(kind="adjudicate", response=SimpleNamespace(usage=SimpleNamespace()))
    assert captured == []


def test_零命中是真实的零不是缺失(monkeypatch) -> None:
    """🔴 "这次一点没命中" 和 "没有这个字段" 含义完全相反。

    第一次调用、或者刚改完 prompt 的那一次，本来就该是 0 命中——那正是我们
    最想看到的信号，不能让它跟"读不到"长得一样。
    """
    captured = _capture(monkeypatch)
    log_call_usage(
        kind="adjudicate",
        response=_response(prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=30000),
    )
    assert captured[0]["cache_hit_tokens"] == 0
    assert captured[0]["cache_hit_ratio"] == 0.0


def test_两边都是零不去做除法(monkeypatch) -> None:
    """除零不许炸。"""
    captured = _capture(monkeypatch)
    log_call_usage(
        kind="adjudicate",
        response=_response(prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=0),
    )
    assert "cache_hit_ratio" not in captured[0]


def test_字段类型不对当成没有(monkeypatch) -> None:
    """SDK 偶尔会给 None 或字符串。拿它去做加法会当场炸在一局游戏中间。"""
    captured = _capture(monkeypatch)
    log_call_usage(
        kind="adjudicate",
        response=_response(prompt_tokens="很多", prompt_cache_hit_tokens=None),
    )
    assert captured == []
