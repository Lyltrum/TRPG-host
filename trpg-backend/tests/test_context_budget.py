"""上下文预算观测（`keeper/context_budget.py`）。

这一层只观测、不改行为，所以用例分两类：**数得对不对**，以及
**🔴 会不会把剧本正文写进日志**——后者是版权红线，比数得准重要。
"""

from __future__ import annotations

import structlog

from app.core.keeper.context_budget import (
    block_heading,
    log_system_prompt,
    log_turn_input,
    measure,
    measure_capability_blocks,
)


def _capture(monkeypatch) -> list[tuple[str, dict]]:
    """截下 structlog 的输出，供断言检查日志里到底有什么。"""
    captured: list[tuple[str, dict]] = []

    class _Spy:
        def info(self, event: str, **kwargs: object) -> None:
            captured.append((event, dict(kwargs)))

    monkeypatch.setattr("app.core.keeper.context_budget.logger", _Spy())
    return captured


# ── 数得对不对 ───────────────────────────────────


def test_每段都被数到_包括空的那些() -> None:
    """🔴 空段保留成 0，不许从结果里消失。

    「这一段这次是空的」本身是信息——分头时角色卡不给叙事器、开局时账本还没有
    内容，都该看得出是 0，而不是让人以为这个字段不存在。
    """
    sizes = measure({"剧本": "12345", "账本": "", "历史": "abc"})
    assert sizes == {"剧本": 5, "账本": 0, "历史": 3}


def test_能力块按标题归因() -> None:
    """「十片能力各占多少」正是这次观测最想回答的问题。"""
    blocks = [
        (10.0, "## 位置\n大家在门厅\n\n"),
        (46.0, "## 重复检定\n侦察 ×2\n\n"),
    ]
    sizes = measure_capability_blocks(blocks)
    assert set(sizes) == {"位置", "重复检定"}
    assert sizes["位置"] == len(blocks[0][1])


def test_读不出标题的块归到问号而不是丢掉() -> None:
    """丢掉的话，总和对不上，而且没人知道少了什么。"""
    sizes = measure_capability_blocks([(1.0, "没有标题的一段")])
    assert sizes == {"?": len("没有标题的一段")}


def test_标题读回来的就是拼进去的那个() -> None:
    assert block_heading("## 悬而未决\n正文") == "悬而未决"
    assert block_heading("正文") == "?"


# ── 🔴 版权红线：日志里只许有数字 ──────────────────


def test_日志里不出现任何一段正文(monkeypatch) -> None:
    """🔴 这些段落里有第三方模组正文。日志只允许字符数、段名、能力名。

    这条是本模块存在的**前提**：观测本身把正文抄进日志，等于把版权红线
    从「只在 `模组资料/` 一处」变成了「还在日志文件里」。
    """
    captured = _capture(monkeypatch)
    秘密 = "米-戈把矿工的脑子装进了罐子里"

    log_turn_input(
        room_id="r1",
        keeper_view=True,
        segments={"历史窗口L3": 秘密, "事实账本L1": 秘密},
        blocks=[(1.0, f"## 悬而未决\n{秘密}\n\n")],
    )
    log_system_prompt(kind="adjudicate", module_title="林中屋", segments={"剧本全文": 秘密})

    assert len(captured) == 2
    for _event, payload in captured:
        assert 秘密 not in repr(payload)
        # 段名和能力名是允许的（它们是字段名不是正文）
    turn = captured[0][1]
    assert turn["segment_chars"]["历史窗口L3"] == len(秘密)
    assert turn["capability_chars"]["悬而未决"] == len(f"## 悬而未决\n{秘密}\n\n")


def test_裁决与叙事分开记(monkeypatch) -> None:
    """两份的分布不同（裁决多出 keeper_only 的块和角色卡），
    合起来记会平均成一个谁都不是的数。"""
    captured = _capture(monkeypatch)
    log_turn_input(room_id="r1", keeper_view=True, segments={"角色卡": "xxx"}, blocks=[])
    log_turn_input(room_id="r1", keeper_view=False, segments={"角色卡": ""}, blocks=[])

    assert [payload["keeper_view"] for _e, payload in captured] == [True, False]
    assert captured[0][1]["total_chars"] == 3
    assert captured[1][1]["total_chars"] == 0


def test_总数等于各段之和(monkeypatch) -> None:
    """总数自己算，不让读日志的人去加——加错了不会有人发现。"""
    captured = _capture(monkeypatch)
    log_turn_input(
        room_id=None,
        keeper_view=True,
        segments={"a": "1234", "b": "56"},
        blocks=[(1.0, "## 甲\n789\n")],
    )
    payload = captured[0][1]
    assert payload["total_chars"] == 6
    assert payload["capability_total"] == len("## 甲\n789\n")


def test_structlog_没被真的调用过(monkeypatch) -> None:
    """替身装对了没有——没装对的话上面那条版权用例是**自证的假绿**：
    它检查的是一个从来没被写入过的列表。"""
    real = structlog.get_logger()
    assert real is not None
    captured = _capture(monkeypatch)
    log_system_prompt(kind="narrate", module_title="t", segments={"剧本全文": "x"})
    assert len(captured) == 1, "替身没接上，上面的断言就什么都没检查"
