"""同一处反复检定的记账与注入（2026-08-16 真机）。

实测一局 17 次检定里侦察占 9 次，反复砸在同一个目标上——「看清地下室里有什么」
4 次、墙洞里又 5 次，每失败一次守秘人就加一层新遮挡。规则 1d 只写了「失败改
代价」，模型把"代价"实现成了再加一道帘子。

判据与"为什么注入不拦"写在 `attempts.py`。这里守两件事：**数得对**、
**只说当前这一处**。
"""

from __future__ import annotations

from app.core.keeper.capabilities.skill_check.attempts import (
    CHECK_ATTEMPTS_KEY,
    format_attempts,
    load_attempts,
    record_attempt,
)
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.contract.registry import SituationContext
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY

_MODULE = ScenarioModule.model_validate(
    {
        "meta": {"id": "attempts-fixture", "title": "试验模组"},
        "kp_truth": {"summary": "无。"},
        "player_intro": "你站在门口。",
        "nodes": [
            {"id": "hall", "title": "门厅", "kp_text": "空的。"},
            {"id": "cellar", "title": "地窖", "kp_text": "也是空的。"},
        ],
    }
)


def _ctx(state: dict) -> SituationContext:
    return SituationContext(module=_MODULE, keeper_state=state)


def test_counting_separates_success_from_failure() -> None:
    table: dict[str, dict[str, int]] = {}
    table = record_attempt(table, "hall", "侦察", succeeded=False)
    table = record_attempt(table, "hall", "侦察", succeeded=False)
    table = record_attempt(table, "hall", "侦察", succeeded=True)
    assert table["hall|侦察"] == {"n": 3, "fail": 2}


def test_recording_does_not_mutate_the_input() -> None:
    """🔴 返回新表：调用方要整列写回 `keeper_state`，原地改会让"读-改-写"
    在并发下把别人刚写的键盖掉。"""
    table = {"hall|侦察": {"n": 1, "fail": 1}}
    record_attempt(table, "hall", "侦察", succeeded=False)
    assert table == {"hall|侦察": {"n": 1, "fail": 1}}


def test_the_block_only_mentions_where_they_are_standing() -> None:
    """🔴 别处掷过多少跟这一拍的判断无关——全列出来是拿噪音换信息。"""
    state = {
        CURRENT_NODE_KEY: "hall",
        CHECK_ATTEMPTS_KEY: {
            "hall|侦察": {"n": 3, "fail": 3},
            "cellar|聆听": {"n": 4, "fail": 4},
        },
    }
    text = format_attempts(_ctx(state))
    assert "侦察" in text and "3 次" in text
    assert "聆听" not in text


def test_a_single_attempt_is_not_worth_mentioning() -> None:
    """掷过一次是常态。说出来只会让模型以为"这里已经查透了"。"""
    state = {CURRENT_NODE_KEY: "hall", CHECK_ATTEMPTS_KEY: {"hall|侦察": {"n": 1, "fail": 1}}}
    assert format_attempts(_ctx(state)) == ""


def test_off_the_map_renders_nothing() -> None:
    """人不在任何节点上时没有"这一处"可言，不硬凑。"""
    state = {CHECK_ATTEMPTS_KEY: {"hall|侦察": {"n": 5, "fail": 5}}}
    assert format_attempts(_ctx(state)) == ""


def test_the_block_says_what_to_do_not_just_the_number() -> None:
    """🔴 光给数字等于没给：模型不知道该拿它干什么。

    **变异检验**：把 `format_attempts` 末尾那段「同一件事不要一再要检定」删掉，
    这条当场红——数字还在，指令没了。
    """
    state = {CURRENT_NODE_KEY: "hall", CHECK_ATTEMPTS_KEY: {"hall|侦察": {"n": 4, "fail": 3}}}
    text = format_attempts(_ctx(state))
    assert "不要再要同一个检定" in text
    # 走得通的出路必须写出来——否则这条就变成"卡死玩家"
    assert "情境实质改变" in text


def test_a_bad_record_never_breaks_the_block() -> None:
    """脏记录只是参考材料，不该炸掉整块局面。"""
    for bad in ({"hall|侦察": "三次"}, {"hall|侦察": {"n": None}}, "不是字典"):
        assert load_attempts({CHECK_ATTEMPTS_KEY: bad}) in ({}, {"hall|侦察": {"n": 0, "fail": 0}})
