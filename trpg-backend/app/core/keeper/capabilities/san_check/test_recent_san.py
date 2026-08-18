"""同一个来源反复掷理智的记账与注入（2026-08-18 真机）。

实测连着三拍为**同一具尸体**掷了三次：「目睹被近距离枪杀」→「目睹爆头后
复活起身」→「目睹复活后蹒跚走向大门」。COC7 里同一来源一场遭遇只掷一次。

`executor.py` 那道「一拍之内只掷一次」的门按**拍**分界，而这三次各自跟在
一句新的玩家发言后面 ⇒ 分属三拍 ⇒ 一次都没拦。判据与"为什么注入不拦"写在
`state.py` 的 `format_recent_san`。这里守三件事：**记得对**、**说得出纪律**、
**新来源不被误伤**。
"""

from __future__ import annotations

from app.core.keeper.capabilities.san_check.state import (
    RECENT_SAN_KEY,
    format_recent_san,
    load_recent_san_reasons,
    record_san_reason,
)
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.contract.registry import SituationContext

_MODULE = ScenarioModule.model_validate(
    {
        "meta": {"id": "recent-san-fixture", "title": "试验模组"},
        "kp_truth": {"summary": "无。"},
        "player_intro": "你在俱乐部里。",
        "nodes": [{"id": "club", "title": "俱乐部", "kp_text": "空的。"}],
    }
)

#: 真机那三次的原文。
_THE_REAL_THREE = (
    "目睹那个中枪的人被近距离枪杀，脑浆溅到附近",
    "目睹那个中枪的人被爆头后复活起身",
    "目睹那个中枪的人复活后蹒跚走向大门",
)


def _ctx(state: dict) -> SituationContext:
    return SituationContext(module=_MODULE, keeper_state=state)


def test_the_three_rolls_from_the_real_session_all_show_up() -> None:
    """🔴 **变异检验**：把 `_remember_san_reasons` 的调用删掉，或者把
    `render_recent_san` 从 `situations` 里摘掉，这条当场红。"""
    recent: list[str] = []
    for reason in _THE_REAL_THREE:
        recent = record_san_reason(recent, reason)
    text = format_recent_san(_ctx({RECENT_SAN_KEY: recent}))
    for reason in _THE_REAL_THREE:
        assert reason in text


def test_recording_does_not_mutate_the_input() -> None:
    """返回新表：调用方要整列写回 `keeper_state`，原地改会在并发下盖掉别人的键。"""
    recent = ["目睹尸体复活"]
    record_san_reason(recent, "目睹另一具尸体")
    assert recent == ["目睹尸体复活"]


def test_only_the_last_few_are_kept() -> None:
    """留太多会把很久以前的事翻出来当"刚掷过"。"""
    recent: list[str] = []
    for i in range(9):
        recent = record_san_reason(recent, f"第{i}件事")
    assert len(recent) == 4
    assert recent[-1] == "第8件事"
    assert "第0件事" not in recent


def test_an_empty_reason_does_not_take_a_slot() -> None:
    """没有理由的一条帮不了模型判断来源，只会挤掉一格有用的。"""
    assert record_san_reason(["目睹尸体复活"], "   ") == ["目睹尸体复活"]


def test_nothing_rolled_yet_renders_nothing() -> None:
    """一次都没掷过时整块不渲染——空标题是纯噪音。"""
    assert format_recent_san(_ctx({})) == ""
    assert format_recent_san(_ctx({RECENT_SAN_KEY: []})) == ""


def test_the_block_says_what_to_do_not_just_the_list() -> None:
    """🔴 光列出来等于没给：模型不知道该拿它干什么。

    **变异检验**：删掉 `format_recent_san` 末尾那两段纪律，这条当场红。
    """
    text = format_recent_san(_ctx({RECENT_SAN_KEY: ["目睹尸体复活"]}))
    assert "同一个来源不要重复检定" in text
    # 走得通的出路必须写出来，否则这条门会把真正的新来源也压掉
    assert "换成新的来源照掷" in text


def test_the_discipline_names_the_exact_shape_that_went_wrong() -> None:
    """真机那三次的差别全是「同一个东西又动了一下」——纪律要直接点破这种形状，
    否则模型会拿"情境升级了"给自己开豁免（那正是它上一拍自己写的叙事）。"""
    text = format_recent_san(_ctx({RECENT_SAN_KEY: ["目睹尸体复活"]}))
    assert "都还是它" in text


def test_a_bad_record_never_breaks_the_block() -> None:
    """脏记录只是参考材料，不该炸掉整块局面。"""
    for bad in ("不是列表", {"a": 1}, None, 3):
        assert load_recent_san_reasons({RECENT_SAN_KEY: bad}) == []
    assert load_recent_san_reasons({RECENT_SAN_KEY: ["", "  ", "真的一条"]}) == ["真的一条"]


def test_the_key_is_reserved_so_the_model_cannot_forge_it() -> None:
    """保留键：不在白名单里的话模型能自己写一份影子账本（`exec/40` ④）。"""
    from app.core.keeper.capabilities import reserved_state_keys

    assert RECENT_SAN_KEY in reserved_state_keys()
