"""流式叙事的编排：把「切边界」「纪律层」「泄密守门」「长度」串成一条路
（`exec/28` 第 3 步）。

## 为什么编排在这一层

非流式的三步是 `agent._finalize_prose` 里的 `scrub → scrub_meta_leaks →
clip_narration`，横跨两片能力（`narration` 与 `access`）。流式版同理，所以
它也必须待在**骨架层**：

- `ProseStreamer` 只知道"一段文本什么时候可以安全交出去"，它待在 `narration`
  能力里，**不能**去 import `access` 的泄密守门——那是架构测试禁止的跨能力依赖；
- 泄密守门也不该反过来知道流式的存在。

于是两片能力各自保持无知，由这里把它们接起来。加一条新的段级规则时，改的
也是这里，不是任何一片能力。

## 🔴 长度：这里跟非流式**有意不等价**

非流式是 `clip_narration`——生成完再砍，超出的 token 已经付过钱了。流式改成
**推到上限就不再推，并结束这次生成**。这是有意的行为差异，不是等价性缺口：

- 玩家看到的仍然不超过 `max_chars`；
- 省掉超出部分的解码时间与费用；
- 代价是"第一句就超长"这种极端情况下，非流式会硬裁 + 省略号，流式则整句不推。

所以 `tests/test_prose_stream_equivalence.py` 覆盖的是 `scrub`，**不覆盖 clip**。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import structlog

from app.core.keeper.access.leak_guard import LeakHit, drop_leaking_sentences, log_leak_hits
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.narration.prose_stream import ProseStreamer
from app.core.llm_tape import StreamCall

logger = structlog.get_logger()


class NarrationStream:
    """一次流式叙事。迭代产出**已经可以推给玩家**的文本片段。

    迭代结束后：
      `.text`      玩家实际收到的全文（= 所有片段拼接）
      `.raw`       模型原始输出，落库/记账用它
      `.truncated` 是否因为到达 `max_chars` 而提前收尾
    """

    def __init__(
        self,
        call: StreamCall,
        *,
        module: ScenarioModule,
        action_intent: bool,
        confused: bool,
        max_chars: int,
        room_id: str | None = None,
    ) -> None:
        self._call = call
        self._module = module
        self._max_chars = max_chars
        self._room_id = room_id
        self._streamer = ProseStreamer(action_intent=action_intent, confused=confused)
        self._pieces: list[str] = []
        self._hits: list[LeakHit] = []
        self._truncated = False

    @property
    def text(self) -> str:
        return "".join(self._pieces)

    @property
    def raw(self) -> str:
        return self._call.text

    @property
    def truncated(self) -> bool:
        return self._truncated

    async def __aiter__(self) -> AsyncIterator[str]:
        async for delta in self._call:
            piece = self._admit(self._streamer.feed(delta))
            if piece:
                yield piece
            if self._truncated:
                # 到量了就不再往下读——超出部分的解码时间与费用直接省掉
                break
        if not self._truncated:
            tail = self._admit(self._streamer.finish())
            if tail:
                yield tail
        log_leak_hits(self._hits, room_id=self._room_id)

    def _admit(self, piece: str) -> str:
        """段级守门：泄密 → 长度。返回真正可以推出去的部分。"""
        if not piece:
            return ""
        cleaned, hits = drop_leaking_sentences(piece, self._module)
        self._hits.extend(hits)
        if not cleaned:
            return ""

        room = self._max_chars - len(self.text)
        if room <= 0:
            self._truncated = True
            return ""
        if len(cleaned) > room:
            # 🔴 不在这里硬裁半句：段的边界就是句的边界，切进去等于把一句话
            # 拦腰截断发给玩家。宁可这一句不发，也不发半句。
            self._truncated = True
            logger.info(
                "keeper_narration_stream_truncated",
                room_id=self._room_id,
                emitted=len(self.text),
                limit=self._max_chars,
                dropped=len(cleaned),
            )
            return ""

        self._pieces.append(cleaned)
        return cleaned
