"""COC7 建卡期年龄修正模块（`app/core/coc7/age.py`，迁移自 coc-char-gen
`js/plugins/age.js`）的单元测试：年龄分档表边界 + 减值分摊算法 + EDU 改进
检定的服务端权威掷骰。
"""

import random

import pytest

from app.core.coc7.age import (
    apply_app_loss,
    distribute_scd_loss,
    get_age_modifiers,
    roll_edu_improvement,
)


def test_age_table_band_boundaries() -> None:
    """七档分界值都要落进正确的档——最容易出错的地方就是边界（<= vs <）。"""
    assert get_age_modifiers(15).label == "15–19"
    assert get_age_modifiers(19).label == "15–19"
    assert get_age_modifiers(20).label == "20–39"
    assert get_age_modifiers(39).label == "20–39"
    assert get_age_modifiers(40).label == "40–49"
    assert get_age_modifiers(49).label == "40–49"
    assert get_age_modifiers(89).label == "80–89"


def test_age_table_youth_band_values() -> None:
    """15-19 档：EDU 固定 -5（不是改进检定）、STR+SIZ 合计 -5、幸运掷两次取高。"""
    mod = get_age_modifiers(17)
    assert mod.edu_checks == 0
    assert mod.edu_flat == -5
    assert mod.str_siz_only is True
    assert mod.scd_loss == 5
    assert mod.luck_twice is True
    assert mod.mov_penalty == 0


def test_age_table_middle_band_values() -> None:
    """20-39 档：只做 1 次 EDU 改进检定，没有身体/外貌/移动惩罚——这是官方
    合并档（20 多岁与 30 多岁规则相同），不是漏写。"""
    mod = get_age_modifiers(30)
    assert mod.edu_checks == 1
    assert mod.edu_flat == 0
    assert mod.app_loss == 0
    assert mod.scd_loss == 0
    assert mod.mov_penalty == 0


def test_age_table_oldest_band_values() -> None:
    """80-89 档：EDU 改进 ×4，STR/CON/DEX 合计 -80，APP -25，MOV -5。"""
    mod = get_age_modifiers(85)
    assert mod.edu_checks == 4
    assert mod.app_loss == 25
    assert mod.scd_loss == 80
    assert mod.str_siz_only is False
    assert mod.mov_penalty == 5


def test_age_under_15_is_a_lenient_fallback() -> None:
    """低于 15 岁规则书没给标准数据——不拒绝，全部修正参数归零。"""
    mod = get_age_modifiers(10)
    assert mod.label == "<15"
    assert mod.edu_checks == 0
    assert mod.scd_loss == 0
    assert mod.mov_penalty == 0


def test_age_90_and_above_reuses_the_oldest_band() -> None:
    """90 岁以上沿用 80-89 档的惩罚（age.js 同款处理），只是换个 label。"""
    mod = get_age_modifiers(95)
    assert mod.label == "90+"
    assert mod.edu_checks == 4
    assert mod.scd_loss == 80
    assert mod.mov_penalty == 5


def test_distribute_scd_loss_rotates_through_keys_until_exhausted() -> None:
    """轮转分摊：STR/CON/DEX 依次各减 1、循环直到减完 loss 点——5 点分给
    3 项，第一轮三项各 -1（用掉 3 点），第二轮从 STR 开始再 -1、-1（用掉
    2 点），最终 STR/CON 各 -2、DEX -1。"""
    attrs = {"STR": 50, "CON": 50, "DEX": 50}
    result = distribute_scd_loss(attrs, 5, only_str_siz=False)
    assert result == {"STR": 48, "CON": 48, "DEX": 49}
    assert sum(attrs[k] for k in attrs) - sum(result[k] for k in result) == 5


def test_distribute_scd_loss_only_str_siz_for_youth_band() -> None:
    """青年档（15-19）只在 STR/SIZ 之间轮转，不动 DEX：3 点分给 2 项，
    STR 先减到底（-2），SIZ 补 1 点（-1）。"""
    attrs = {"STR": 50, "SIZ": 50, "DEX": 50}
    result = distribute_scd_loss(attrs, 3, only_str_siz=True)
    assert result == {"STR": 48, "SIZ": 49, "DEX": 50}


def test_distribute_scd_loss_never_drops_below_one() -> None:
    """减到 1 就不能再减——即使剩余 loss 还没分完也要停下。"""
    attrs = {"STR": 2, "CON": 1, "DEX": 1}
    result = distribute_scd_loss(attrs, 10, only_str_siz=False)
    assert result == {"STR": 1, "CON": 1, "DEX": 1}


def test_distribute_scd_loss_zero_loss_is_a_no_op() -> None:
    attrs = {"STR": 50, "CON": 50, "DEX": 50}
    assert distribute_scd_loss(attrs, 0, only_str_siz=False) == attrs


def test_apply_app_loss_clamps_at_one() -> None:
    assert apply_app_loss({"APP": 50}, 20)["APP"] == 30
    assert apply_app_loss({"APP": 10}, 25)["APP"] == 1


def test_roll_edu_improvement_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """d100 > 当前 EDU 才算成功，成功再掷 1d10 当增量。"""
    rolls = iter([80, 6])  # 第一次调用是 d100，第二次是 d10
    monkeypatch.setattr(random, "randint", lambda lo, hi: next(rolls))

    success, roll, gain, new_edu = roll_edu_improvement(edu=50)

    assert (success, roll, gain, new_edu) == (True, 80, 6, 56)


def test_roll_edu_improvement_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """d100 没超过当前 EDU：不成功，EDU 不变，也不会去掷 1d10。"""
    monkeypatch.setattr(random, "randint", lambda lo, hi: 40)

    success, roll, gain, new_edu = roll_edu_improvement(edu=50)

    assert (success, roll, gain, new_edu) == (False, 40, 0, 50)


def test_roll_edu_improvement_caps_at_99(monkeypatch: pytest.MonkeyPatch) -> None:
    """EDU 改进检定不能把 EDU 顶过 99 上限。"""
    rolls = iter([100, 10])
    monkeypatch.setattr(random, "randint", lambda lo, hi: next(rolls))

    success, roll, gain, new_edu = roll_edu_improvement(edu=95)

    assert success is True
    assert new_edu == 99
