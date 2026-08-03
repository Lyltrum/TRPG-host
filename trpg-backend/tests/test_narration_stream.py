"""流式叙事的编排层（`exec/28` 第 3 步）：切边界 → 泄密守门 → 长度。

跟 `test_prose_stream_equivalence.py` 分工不同：那边守的是「流式 scrub 与全量
scrub 逐字节相等」，这里守的是**编排**——三样东西的顺序、段级守门不塞占位、
到量就停止读流。
"""

from __future__ import annotations

import pytest

from app.core.keeper.contract.module_loader import (
    KeeperTruth,
    ModuleFact,
    ModuleMeta,
    ScenarioModule,
)
from app.core.keeper.runtime.narration_stream import NarrationStream

TRUTH = "道格拉斯并没有死而是被囚禁在地窖深处"


def _module() -> ScenarioModule:
    return ScenarioModule(
        meta=ModuleMeta(id="m", title="合成模组"),
        kp_truth=KeeperTruth(summary="真相"),
        player_intro="开场",
        facts=[ModuleFact(id="fact-truth", text=TRUTH, kind="truth", tier="meta")],
    )


class _FakeCall:
    """假的 StreamCall：按块吐出预设文本，并记下被读了多少块。"""

    def __init__(self, text: str, chunk: int = 4) -> None:
        self._full = text
        self._chunk = chunk
        self.text = ""
        self.chunks_read = 0

    async def __aiter__(self):  # noqa: ANN204
        for i in range(0, len(self._full), self._chunk):
            piece = self._full[i : i + self._chunk]
            self.text += piece
            self.chunks_read += 1
            yield piece


async def _collect(stream: NarrationStream) -> list[str]:
    return [p async for p in stream]


def _stream(text: str, **kw) -> tuple[NarrationStream, _FakeCall]:  # noqa: ANN003
    call = _FakeCall(text)
    defaults = {"action_intent": False, "confused": False, "max_chars": 180}
    defaults.update(kw)
    return NarrationStream(call, module=_module(), **defaults), call  # ty: ignore


@pytest.mark.asyncio
async def test_clean_narration_streams_through() -> None:
    text = "他推开门，屋里一片死寂。壁炉还残留着余温。"
    stream, _ = _stream(text)

    pieces = await _collect(stream)

    assert len(pieces) > 1, "应该分多次推出，不是攒到最后一次给"
    assert stream.text == text
    assert stream.raw == text
    assert not stream.truncated


@pytest.mark.asyncio
async def test_leaking_sentence_never_reaches_the_player() -> None:
    """🔴 这是整条流式路径存在的最大风险点：元层泄漏一个字都不许推出去。"""
    text = f"他推开门，屋里一片死寂。{TRUTH}。壁炉还残留着余温。"
    stream, _ = _stream(text)

    pieces = await _collect(stream)

    assert TRUTH not in "".join(pieces)
    assert TRUTH not in stream.text
    assert "壁炉还残留着余温" in stream.text
    # 原始文本仍然完整——落库/记账要的是模型真正说了什么
    assert TRUTH in stream.raw


@pytest.mark.asyncio
async def test_menu_tail_never_reaches_the_player() -> None:
    text = "他推开门，屋里空无一人。\n你可以：搜查抽屉、离开房间。"
    stream, _ = _stream(text)

    assert "你可以" not in "".join(await _collect(stream))


@pytest.mark.asyncio
async def test_emptied_segment_does_not_emit_placeholder_mid_paragraph() -> None:
    """🔴 段级守门删光了就是空串，不许塞「守秘人顿了顿」——那是整段级的决定。

    否则玩家会在一段话中间读到一句占位文案。
    """
    text = f"{TRUTH}。他站在门口没有动。"
    stream, _ = _stream(text)

    pieces = await _collect(stream)

    assert "守秘人顿了顿" not in "".join(pieces)
    assert stream.text == "他站在门口没有动。"


@pytest.mark.asyncio
async def test_stops_reading_the_stream_once_the_limit_is_reached() -> None:
    """🔴 到量就停止读流——省掉超出部分的解码时间与费用，不是先读完再砍。"""
    long_text = "".join(f"这是第{i}句描写，长度大致固定不变。" for i in range(20))
    stream, call = _stream(long_text, max_chars=60)

    await _collect(stream)

    assert stream.truncated
    assert len(stream.text) <= 60
    assert call.chunks_read * 4 < len(long_text), "到量之后不该继续把整条流读完"


@pytest.mark.asyncio
async def test_never_emits_half_a_sentence_at_the_limit() -> None:
    """段的边界就是句的边界：宁可这一句不发，也不发半句。"""
    stream, _ = _stream("短句。这是一个明显更长的句子，长到放不进剩余额度里。", max_chars=8)

    await _collect(stream)

    assert stream.text == "短句。"
    assert stream.truncated
