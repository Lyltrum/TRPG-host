"""无 API Key 时的确定性占位实现。

保证 CI/e2e 不依赖外部服务——这也是它必须存在的理由：测试要能在没有任何
密钥的机器上跑绿。
"""

from app.core.narration.contract import NarrationContext, NarrationOutcome, Narrator


class FallbackNarrator(Narrator):
    """无 API Key 时的确定性占位实现，等价于此前 ws.py 里硬编码的占位文案。"""

    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        # 开场仪式：空串让 WS 层回退到 structured opening.script 粘贴
        # （CI 无 DeepSeek 时仍满足「进局必有开场旁白」）。
        if context.is_opening_ceremony:
            return NarrationOutcome(text="")
        return NarrationOutcome(text=f"守秘人记下了你的行动：「{context.utterance}」……")
