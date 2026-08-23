"""跟模型打交道的全部调用：裁决（幕后）· 叙事（台前）· 章节摘要（后台）。

三次 LLM 往返都在这个文件里——温度、超时、重试次数、JSON mode、
兜底文案，**跟模型有关的旋钮都在这一处**。此前它们散在 `agent.py` 里，跟编排
逻辑混在一起，"想调一下裁决温度"得先在 1400 行里找它。

三次调用（裁决 / 叙事 / 章节摘要）的性格各不相同，前两次的对立正是 v2 架构
的核心：

- **裁决**（`adjudicate`）：温度 0.3、JSON mode、解析失败把错误喂回去重试。
  要的是稳定一致的规则判断，不是创造力。
- **叙事**（`narrate_prose`）：高温、纯文本、没有工具也没有裁决压力。写作本能
  在这里从对抗对象变成生产力。
- **章节摘要**（`summarize_chapter`）：温度 0.2、后台跑、失败只记日志。

v1 把两件事塞进一次调用，实测被写作本能碾压（该掷不掷/线索白给/状态不记，
三轮 prompt 强化无效）——见 `agent.py` 的模块 docstring。
"""

from __future__ import annotations

from time import perf_counter

import structlog
from openai.types.chat import ChatCompletionMessageParam
from pydantic import ValidationError

from app.core.config import get_settings
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.narration.prompts import (
    CHAPTER_SUMMARY_INSTRUCTIONS,
    format_chapter_input,
    format_narrator_input,
)
from app.core.keeper.narration.prose_discipline import clip_narration
from app.core.llm_tape import StreamCall, TapedClient
from app.core.narration.deepseek import deepseek_model

logger = structlog.get_logger()


#: 单次模型往返的超时。真人在等，宁可兜底也不要无限期挂着。
def request_timeout_seconds() -> float:
    """这一处的请求超时。读 settings，**不做模块常量**——常量在 import 那一刻
    就定死，`.env` 与测试都改不动它（同 `deepseek_model()` 的理由）。"""
    return get_settings().keeper_timeout_seconds


#: 裁决解析失败后的重试次数（把校验错误喂回去让它自己改）。
ADJUDICATE_RETRIES = 2

#: DeepSeek 的"思考"字段：裁决与叙事都不需要它，白烧输出 token。
DISABLE_THINKING: dict = {"thinking": {"type": "disabled"}}

#: 裁决整个失败时的兜底 guidance。**它同时是"裁决失败了"的标记**——
#: `turn_policy.classify_turn` 靠比对这段文本决定要不要退回正则分类。
FALLBACK_ADJUDICATE_GUIDANCE = (
    "裁决阶段解析失败。请只依据已知信息稳妥回应，不要编造检定结果或新线索；"
    "可请玩家把行动说得更具体一些。"
)


async def adjudicate(client: TapedClient, instructions: str, situation: str) -> KeeperDecision:
    """阶段1：裁决。JSON mode + pydantic 校验，解析失败把错误喂回去重试。

    温度压低（0.3）：裁决要的是稳定一致的规则判断，不是创造力。
    全部重试仍失败（含空 content）时返回兜底决策，避免整轮静默失败。
    """
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": instructions},
        {"role": "user", "content": situation + "\n\n请输出本轮的裁决 JSON。"},
    ]
    last_error: Exception | None = None
    # 🔴 耗时埋点（`exec/28`）。**纯观测，不改任何行为。**
    #
    # 为什么必须有它：玩家反馈"首字要等 5-6 秒"时，我拿叙事那一拍实测的 1.2s
    # 去对，对不上——因为裁决这一拍全程干等（JSON mode 要完整 JSON 才能解析，
    # 没法流式），它才是大头。项目判据「慢要先定位是哪一拍慢」在这里又应验了
    # 一次，而当时**没有任何数据能分解这两拍**。
    started = perf_counter()
    for attempt in range(1 + ADJUDICATE_RETRIES):
        response = await client.chat.completions.create(
            tape_kind="adjudicate",
            model=deepseek_model(),
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.3,
            extra_body=DISABLE_THINKING,
        )
        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            last_error = ValueError("empty adjudicate response")
            logger.warning(
                "keeper_adjudicate_empty",
                attempt=attempt + 1,
                room_hint=situation[:80],
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "你上一轮返回了空内容。必须输出完整合法的裁决 JSON 对象，"
                        "至少包含 thinking 与 narration_guidance 字段；"
                        "checks/san_checks/hp_changes/state_updates 可为 []。"
                    ),
                }
            )
            continue
        try:
            decision = KeeperDecision.model_validate_json(raw)
            logger.info(
                "keeper_adjudicate_timing",
                elapsed_ms=round((perf_counter() - started) * 1000),
                attempts=attempt + 1,
                raw_len=len(raw),
            )
            return decision
        except ValidationError as exc:
            last_error = exc
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": f"JSON 不符合要求：{exc}。请重新只输出一个合法的裁决 JSON。",
                }
            )
    logger.warning(
        "keeper_adjudicate_fallback",
        error=str(last_error),
        elapsed_ms=round((perf_counter() - started) * 1000),
    )
    return KeeperDecision(
        thinking=f"裁决解析失败兜底：{last_error}",
        narration_guidance=FALLBACK_ADJUDICATE_GUIDANCE,
    )


def build_narration_input(
    situation: str,
    decision: KeeperDecision,
    report: list[str],
    issues: list[str],
    *,
    max_chars: int,
    extra_suffix: str = "",
) -> str:
    """叙事那一拍的 user 消息。流式与非流式共用一份，防止两条路径的 prompt 漂开。"""
    length_hint = (
        f"\n\n【长度硬限】本轮正文不得超过 {max_chars} 字（含标点）。"
        "超长会被系统截断——请一次写完且写短。"
    )
    return (
        format_narrator_input(situation, decision.narration_guidance, report, issues)
        + length_hint
        + extra_suffix
    )


def narrate_prose_stream(
    client: TapedClient,
    instructions: str,
    situation: str,
    decision: KeeperDecision,
    report: list[str],
    issues: list[str],
    *,
    max_tokens: int,
    max_chars: int,
    extra_suffix: str = "",
    tape_key: str | None = None,
) -> StreamCall:
    """阶段3的流式版（`exec/28`）。**不做任何裁剪**——纪律层由调用方按段施加。

    刻意跟 `narrate_prose` 分开而不是加个 `stream=` 开关：那个函数的返回值是
    "已经处理干净的一段话"，而这个返回的是"一条还没过纪律层的原始流"，两者
    的契约不同，合并只会让调用方分不清手上拿的是哪种。

    prompt 与非流式**共用 `build_narration_input`**，所以两条路径不会漂开。
    """
    return client.chat.completions.stream(
        tape_kind="narrate",
        tape_key=tape_key,
        model=deepseek_model(),
        messages=[
            {"role": "system", "content": instructions},
            {
                "role": "user",
                "content": build_narration_input(
                    situation,
                    decision,
                    report,
                    issues,
                    max_chars=max_chars,
                    extra_suffix=extra_suffix,
                ),
            },
        ],
        temperature=0.8,
        max_tokens=max_tokens,
        extra_body=DISABLE_THINKING,
    )


async def narrate_prose(
    client: TapedClient,
    instructions: str,
    situation: str,
    decision: KeeperDecision,
    report: list[str],
    issues: list[str],
    *,
    max_tokens: int,
    max_chars: int,
    extra_suffix: str = "",
    tape_key: str | None = None,
) -> str:
    """阶段3：叙事。max_tokens 限生成；max_chars 代码硬裁（句末优先）。

    `extra_suffix` 追加在 `length_hint` 之后、整段 user_content 的最
    末尾——目前唯一的用途是检定边界硬提醒（见 `_build_check_boundary_
    hint`），放在这个位置是刻意的（近因效应，模型对最后读到的指令
    服从概率更高，跟 `length_hint` 本身的位置选择同理）。
    """
    user_content = build_narration_input(
        situation, decision, report, issues, max_chars=max_chars, extra_suffix=extra_suffix
    )
    response = await client.chat.completions.create(
        tape_kind="narrate",
        tape_key=tape_key,
        model=deepseek_model(),
        messages=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_content},
        ],
        temperature=0.8,
        max_tokens=max_tokens,
        extra_body=DISABLE_THINKING,
    )
    raw = response.choices[0].message.content or ""
    if response.choices[0].finish_reason == "length":
        # 即便关了隐藏推理，正文本身也可能写超——这里留痕方便以后一眼看出
        # 是被 max_tokens 硬砍的，而不是 clip_narration 的优雅裁切。
        logger.warning(
            "keeper_narration_hit_token_limit",
            max_tokens=max_tokens,
            raw_len=len(raw.strip()),
        )
    clipped = clip_narration(raw, max_chars)
    if len(raw.strip()) > max_chars:
        logger.info(
            "keeper_narration_clipped",
            before=len(raw.strip()),
            after=len(clipped),
            limit=max_chars,
        )
    return clipped


async def summarize_chapter(client: TapedClient, history_lines: list[str]) -> str:
    """第三次调用：把一段游戏历史压成一句话梗概（L2 分段摘要）。

    温度更低（0.2）：要的是"发生了什么"的忠实压缩，不是再创作。

    ⚠️ 它是**后台**调用——玩家等的是叙事，不该为"整理笔记"多等几秒。失败由
    调用方吞掉只记日志：它是记忆的锦上添花，不是主路径。

    （补记：抽 llm_calls 时我以为只有裁决与叙事两次往返，写模块 docstring 时
    还写着"全部 LLM 往返"。这一次是在 `_summarize_chapter` 里撞见的——**说
    "全部"之前先 grep 一遍 `completions.create`。**）
    """
    response = await client.chat.completions.create(
        tape_kind="chapter",
        model=deepseek_model(),
        messages=[
            {"role": "system", "content": CHAPTER_SUMMARY_INSTRUCTIONS},
            {"role": "user", "content": format_chapter_input(history_lines)},
        ],
        temperature=0.2,
        extra_body=DISABLE_THINKING,
    )
    return (response.choices[0].message.content or "").strip()
