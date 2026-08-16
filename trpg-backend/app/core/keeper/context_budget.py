"""上下文预算观测：每次调用的各段有多大。

## 为什么先做这个

上下文现在**没有人对总量负责**。局面块是十片能力各自贡献一段
（`SituationBlock`），每片只关心自己那几行合不合理；L1 事实账本与 L2 分段摘要
刻意「不设 limit」（它们必须活过 L3 的 200 条窗口）——于是三段无界增长的东西
同时存在，而加一片新能力会让总量再长一点，**不会有任何东西变红**。

在动手裁剪之前先量，理由是项目自己那条判据：**先量再定形状**。此前所有关于
"上下文够不够"的估算都是拿测试用的迷你剧本（3,687 字符）外推真实模组
（20k–63k 字符）——那正是「拿一个样本当通例」。

## 🔴 只记数字，绝不记内容

这些段落里有第三方模组正文（`render_full` 就是剧本全文）。**版权红线：日志里
只允许出现字符数、段名、能力名，一个字的正文都不许进。** 本模块的函数签名
因此只往外吐 `int`，`measure` 收到的文本用完即弃。`test_context_budget.py`
有一条用例专门盯着这件事。

## 单位是字符，不是 token

字符数是**精确**的，token 数需要 tokenizer 而且随模型变。中文大致 1.5 字符
≈ 1 token，要折算自己乘——但**别把估算值写进日志**，那会让人把估算当实测。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import structlog

logger = structlog.get_logger()

#: 能力块的标题行形如 `## 重复检定\n……`（`capabilities.situation_blocks` 拼的）。
#: 从成品文本里把标题读回来，是为了**按能力归因**——「十片能力各占多少」正是
#: 这次观测最想回答的问题，而 `situation_blocks` 的返回值只有 (order, 文本)。
_HEADING_PREFIX = "## "


def block_heading(block: str) -> str:
    """从一块成品能力块里读回它的标题。读不出来就归到 `?`。

    解析回自己刚拼好的东西确实不优雅，但它**只影响日志上的一个标签**：
    解析错了得到的是一行标签不对的日志，不是一次行为改变。为此改
    `situation_blocks` 的返回类型要动它的每一个调用方，不值得。
    """
    first_line = block.lstrip().split("\n", 1)[0]
    if first_line.startswith(_HEADING_PREFIX):
        return first_line[len(_HEADING_PREFIX) :].strip() or "?"
    return "?"


def measure(segments: Mapping[str, str]) -> dict[str, int]:
    """各段的字符数。空段也保留成 0——**"这一段这次是空的"本身是信息**，
    从日志里消失会让人以为它一直不存在。"""
    return {name: len(text or "") for name, text in segments.items()}


def measure_capability_blocks(blocks: Sequence[tuple[float, str]]) -> dict[str, int]:
    """按能力标题拆开的局面块大小。同名标题合并（正常情况下不会有）。"""
    sizes: dict[str, int] = {}
    for _order, text in blocks:
        heading = block_heading(text)
        sizes[heading] = sizes.get(heading, 0) + len(text)
    return sizes


def log_turn_input(
    *,
    room_id: str | None,
    keeper_view: bool,
    segments: Mapping[str, str],
    blocks: Sequence[tuple[float, str]],
) -> None:
    """记一轮的局面块账。

    `keeper_view` 分开记：裁决那份比叙事那份多（`keeper_only` 的块 + 角色卡），
    合起来记会把两个不同的分布平均成一个谁都不是的数。
    """
    sizes = measure(segments)
    per_capability = measure_capability_blocks(blocks)
    logger.info(
        "keeper_context_turn_input",
        room_id=room_id,
        keeper_view=keeper_view,
        total_chars=sum(sizes.values()),
        segment_chars=sizes,
        capability_chars=per_capability,
        capability_total=sum(per_capability.values()),
    )


def log_system_prompt(*, kind: str, module_title: str, segments: Mapping[str, str]) -> None:
    """记 system prompt 的账。

    它**每个模组只建一次**（`KeeperAgent.__init__`），所以这行日志一局只出现
    一次——但它恰恰是最大的那一段（剧本全文），必须量。

    `module_title` 是模组标题，不是正文：标题在 UI 上本来就对玩家可见，不属于
    版权红线里的"正文"。没有它就分不清这行日志说的是哪个模组。
    """
    sizes = measure(segments)
    logger.info(
        "keeper_context_system_prompt",
        kind=kind,
        module_title=module_title,
        total_chars=sum(sizes.values()),
        segment_chars=sizes,
    )
