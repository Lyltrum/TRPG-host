"""把彼此独立的 LLM 调用并起来跑（`exec/30` 步骤 1）。

## 为什么

实测一条 24 页 PDF 的转换链 394 秒里 **~260 秒是白等的串行**：关系发现是
8 批 × 2 遍、彼此完全独立，却一个一个跑，光它就占整条链 70%。这些调用之间
没有任何数据依赖，串着跑纯粹是代码写成了 `for`。

同族于 `exec/26` #68：**流式只压缩"一段之内"的等待，压不掉"排在前面那几段"
的等待——串行才是大头。**

## 为什么是线程不是 asyncio

转换链（`scripts/module_probe/`）整条是同步的，由 `asyncio.to_thread` 在工作
线程里跑。改成 async 要波及三个脚本的 CLI 入口，而这里等的全是网络 IO，
线程池够用且改动面小。

## 🔴 调用方必须给稳定子键

并行之后完成顺序不确定，**按序回放的磁带当场炸**。所以每个并行任务都要带一个
跑多少次都算得出同一个值的 key（`relations:pass1:batch3`），磁带按 key 回放。
这两件事必须一起做，见 `app/core/llm_tape.TapeSession`。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

#: 并发上限的兜底值。DeepSeek 那边没有公开的硬并发限制，取一个既能吃掉大头
#: 又不至于把 provider 打满的数——8 批关系发现一轮就跑完。
DEFAULT_CONCURRENCY = 8


def resolve_concurrency(explicit: int | None = None) -> int:
    """并发度：显式参数 > 环境变量 > 默认值。

    留环境变量是因为这三个脚本也当 CLI 单独跑，那时读不到后端 Settings。
    """
    if explicit is not None:
        return max(1, explicit)
    raw = os.environ.get("MODULE_IMPORT_LLM_CONCURRENCY")
    if not raw:
        return DEFAULT_CONCURRENCY
    try:
        return max(1, int(raw))
    except ValueError as exc:
        raise ValueError(f"MODULE_IMPORT_LLM_CONCURRENCY 不是整数：{raw!r}") from exc


def run_parallel[T](jobs: Sequence[Callable[[], T]], *, concurrency: int | None = None) -> list[T]:
    """并发跑 `jobs`，**按输入顺序**返回结果。

    - 任何一个任务抛异常 → 整体抛出（取索引最小的那个，让报错稳定可复现）；
      其余任务不再取消，反正它们已经在飞了，等它们结束比留悬空线程干净。
    - `concurrency <= 1` 或只有一个任务 → 直接在当前线程串行跑。保留这条路是
      因为**并行会把异常栈搅乱**，排查时能一键退回串行很值钱。
    """
    if not jobs:
        return []
    workers = min(resolve_concurrency(concurrency), len(jobs))
    if workers <= 1:
        return [job() for job in jobs]

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="module-import") as pool:
        futures = [pool.submit(job) for job in jobs]
        # list(...) 会在第一个异常处停下，而我们要的是「全部跑完，再报最靠前的
        # 那个错」——否则同一份输入两次跑出不同的报错。
        outcomes: list[T | BaseException] = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except Exception as exc:  # noqa: BLE001 — 收集起来统一处理
                outcomes.append(exc)

    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome
    return [o for o in outcomes if not isinstance(o, BaseException)]
