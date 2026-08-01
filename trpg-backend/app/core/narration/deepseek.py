"""接真实大模型的单轮叙事实现。

DeepSeek 的 base_url / model 常量也放在这里——它们是**这个实现**的细节，
不是叙事契约的一部分。`ai_actor` 与 `background_writer` 也从这里取。
"""

from openai.types.chat import ChatCompletionMessageParam

from app.core.llm_tape import build_llm_client
from app.core.narration.contract import NarrationContext, NarrationOutcome, Narrator

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
# 2026-07 DeepSeek 仅支持 v4：deepseek-v4-flash / deepseek-v4-pro（deepseek-chat 已 400）
DEEPSEEK_MODEL = "deepseek-v4-pro"
_REQUEST_TIMEOUT_SECONDS = 30.0

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
            api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=_REQUEST_TIMEOUT_SECONDS
        )

    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        response = await self._client.chat.completions.create(
            tape_kind="simple_narrate",
            model=DEEPSEEK_MODEL,
            messages=_build_messages(context),
            # deepseek-v4-pro 默认带隐藏推理（reasoning_content），单轮场景描写不
            # 需要链式思考，关掉省 token 和延迟（keeper/agent.py 同款设置，那边
            # 还额外解决了推理挤占 max_tokens 导致正文被硬砍的问题）。
            extra_body={"thinking": {"type": "disabled"}},
        )
        return NarrationOutcome(text=response.choices[0].message.content or "")
