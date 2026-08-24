"""给占位实现加一点延迟的装饰器实现。"""

import asyncio

from app.core.narration.contract import (
    CheckResultCallback,
    NarrationContext,
    NarrationOutcome,
    Narrator,
)


class DelayedNarrator(Narrator):
    """测试专用包装：narrate 前人为等待一段时间（issue #107 测试钩子）。

    为什么需要它：FallbackNarrator 同步秒回，`action.submit` 的房间锁窗口只有
    微秒级——e2e 里两个客户端"同时提交"永远压不中 `ACTION_IN_PROGRESS`，
    锁相关的验收全成了测不到的死代码。配置 `narrator_delay_seconds`（生产
    永远是 0）后锁窗口被人为拉长，并发拒绝路径才能被稳定命中。
    """

    def __init__(self, inner: Narrator, delay_seconds: float) -> None:
        self._inner = inner
        self._delay_seconds = delay_seconds

    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        await asyncio.sleep(self._delay_seconds)
        return await self._inner.narrate(context)

    async def resolve_check(
        self,
        room_id: str,
        player_id: str,
        check_request_id: str,
        on_result: CheckResultCallback | None = None,
        roll_value: int | None = None,
    ) -> NarrationOutcome:
        await asyncio.sleep(self._delay_seconds)
        return await self._inner.resolve_check(
            room_id, player_id, check_request_id, on_result, roll_value=roll_value
        )
