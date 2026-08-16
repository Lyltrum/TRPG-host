"""每次调用的 token 账，重点是**前缀缓存命中了没有**。

## 为什么要量这个

裁决的 system prompt 是 20.6k–34.4k 字符,**每次调用一字不变**,而且
`KeeperAgent` 按模组缓存（`room_aware.py`）⇒ 玩同一个模组的**所有房间共用
同一份 system prompt 字符串**。这是前缀缓存最理想的形状：一局 51 次裁决,
等于同一段前缀发了 51 遍；房间越多,命中率越高。

DeepSeek 的上下文缓存是**自动的**,不需要在请求里标记任何东西。也就是说
它很可能**已经在命中了,只是没人看过**。所以这里不做"接入",只做"量"——
先知道现状,再决定要不要动手。这跟 `keeper/context_budget.py` 是同一个路子。

## 它会怎么悄悄坏掉

前缀缓存的失效方式是**静默**的：往 system prompt 里塞一个房间名、一个时间戳、
一个玩家昵称,跨房间缓存当场归零,而**没有任何测试会变红**——正是这个项目
反复吃亏的那类缺陷。这行日志就是发现它的唯一途径。
（另有 `test_prompts.py` 一条守护测试钉住"同一个模组建两次,system prompt
逐字节相同"。）

## 🔴 只有非流式那条路

叙事走流式,而流式要拿 usage 得在请求里加 `stream_options={"include_usage": True}`
——那会改变请求参数,进而改变磁带的 request_digest,让现有磁带全部作废。
不值得为一个观测重录。裁决（大前缀、一局 51 次）走的正是非流式这条,先量它。

## 字段是 DeepSeek 特有的,所以读法必须是防御性的

`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` 不是 OpenAI 标准字段。
换 provider、磁带回放的假响应、SDK 升级都可能让它们不存在——**取不到就不记
那两项,绝不报错**：观测坏掉不能连累一局游戏。
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger()


def _maybe_int(source: Any, name: str) -> int | None:
    """取一个整数字段，取不到就 None。**不区分"没有这个字段"和"值是 0"**——
    前者要从日志里消失，后者是真实的 0，两者含义完全不同。"""
    value = getattr(source, name, None)
    return value if isinstance(value, int) else None


def log_call_usage(*, kind: str, response: Any) -> None:
    """记一次调用的 token 账。任何取不到的字段都直接略过。

    `kind` 就是磁带的 `tape_kind`（adjudicate / character_background /
    equipment_check…）——不同用途的前缀稳定性天差地别，混在一起记看不出问题。
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return

    fields = {
        "prompt_tokens": _maybe_int(usage, "prompt_tokens"),
        "completion_tokens": _maybe_int(usage, "completion_tokens"),
        "cache_hit_tokens": _maybe_int(usage, "prompt_cache_hit_tokens"),
        "cache_miss_tokens": _maybe_int(usage, "prompt_cache_miss_tokens"),
    }
    present: dict[str, float] = {k: v for k, v in fields.items() if v is not None}
    if not present:
        return

    hit = present.get("cache_hit_tokens")
    miss = present.get("cache_miss_tokens")
    if hit is not None and miss is not None and (hit + miss) > 0:
        # 命中率自己算好，别让读日志的人去除——除错了不会有人发现。
        present["cache_hit_ratio"] = round(hit / (hit + miss), 3)

    logger.info("llm_call_usage", kind=kind, **present)
