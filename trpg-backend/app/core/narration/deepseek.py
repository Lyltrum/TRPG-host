"""接真实大模型的单轮叙事实现。

DeepSeek 的 base_url / model 常量也放在这里——它们是**这个实现**的细节，
不是叙事契约的一部分。`ai_actor` 与 `background_writer` 也从这里取。
"""

from openai.types.chat import ChatCompletionMessageParam

from app.core.config import get_settings
from app.core.llm_tape import build_llm_client
from app.core.narration.contract import NarrationContext, NarrationOutcome, Narrator


def deepseek_base_url() -> str:
    """DeepSeek 的 API 地址。**每次读一遍 settings，不做模块级常量。**

    做成函数而不是常量，是为了让环境变量真的说了算：模块常量在 import 的那一
    刻就定死了，测试里改环境变量、部署时换一份 `.env`，两种都不会生效——而
    "看着能配、其实配不动"正是这里刚犯过的错（见 `config.deepseek_model`）。
    `get_settings()` 带 lru_cache，这一层读取是免费的。
    """
    return get_settings().deepseek_base_url


def deepseek_model() -> str:
    """对局这条链用哪个模型。默认 `deepseek-v4-flash`，由 `DEEPSEEK_MODEL` 覆盖。

    🔴 **它管着整条对局链**：裁决 / 叙事 / AI 玩家 / 装备审核 / 复盘 / 章节
    摘要六个调用方都从这里取，换它就是换全部。

    模组导入**不走这里**（`scripts/module_probe/probe.py` 自己一份），那是另一
    条线、另一种负载，换它要单独验一次导入。
    """
    return get_settings().deepseek_model


def _request_timeout_seconds() -> float:
    """这一处的请求超时。读 settings，**不做模块常量**——常量在 import 那一刻
    就定死，`.env` 与测试都改不动它（同 `deepseek_model()` 的理由）。"""
    return get_settings().deepseek_timeout_seconds


_SYSTEM_PROMPT = (
    "你是一名跑《克苏鲁的呼唤》（COC7）跑团的守秘人（KP）。"
    "根据玩家刚才的行动，生成一段简短（150 字以内）的叙事回应，"
    "延续克苏鲁风格的诡异、压抑、未知的恐怖氛围。"
    "你只做场景描写和气氛推进：不要替玩家做决定，不要掷骰，"
    "不要裁定行动是否成功——那些是规则引擎的职责，不属于你。"
)


def _build_messages(context: NarrationContext) -> list[ChatCompletionMessageParam]:
    """把 `NarrationContext` 拼成 chat completion 的 messages。

    抽成独立的纯函数，是为了不发真实网络请求也能单测 prompt 组装是否正确
    （`tests/test_narrator.py`）。
    """
    lines = []
    if context.module_title:
        lines.append(f"当前模组：{context.module_title}")
    if context.recent_actions:
        lines.append("最近的行动：")
        lines.extend(context.recent_actions)
    lines.append(f"{context.player_nickname}：{context.utterance}")
    user_content = "\n".join(lines)

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


class DeepSeekNarrator(Narrator):
    """接真实 DeepSeek 大模型的叙事生成实现。

    超时/请求失败一律让异常抛出去，本模块不吞——由调用方（WS 层，#107 后续
    批次接线）决定失败时怎么回应玩家（比如退回占位文案，或者回一条 error
    事件），职责不在这里。
    """

    def __init__(self, api_key: str) -> None:
        self._client = build_llm_client(
            api_key=api_key, base_url=deepseek_base_url(), timeout=_request_timeout_seconds()
        )

    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        response = await self._client.chat.completions.create(
            tape_kind="simple_narrate",
            model=deepseek_model(),
            messages=_build_messages(context),
            # v4 默认带隐藏推理（reasoning_content），单轮场景描写不需要链式
            # 思考，关掉省 token 和延迟（keeper/agent.py 同款设置，那边还额外
            # 解决了推理挤占 max_tokens 导致正文被硬砍的问题）。
            extra_body={"thinking": {"type": "disabled"}},
        )
        return NarrationOutcome(text=response.choices[0].message.content or "")
