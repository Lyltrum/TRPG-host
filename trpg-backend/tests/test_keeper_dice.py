"""keeper agent 的掷骰与 COC7 成功等级判定（app/core/keeper/dice.py）。

纯函数层：固定 seed 的 `random.Random` 让每次掷骰完全可复现——这是"服务端
权威掷骰"的可测试性基础。
"""

import random

import pytest

from app.core.keeper import dice


class TestRollDiceExpr:
    def test_plain_number(self) -> None:
        assert dice.roll_dice_expr("0", random.Random(1)) == 0
        assert dice.roll_dice_expr("5", random.Random(1)) == 5

    def test_ndm_in_range(self) -> None:
        rng = random.Random(42)
        for _ in range(100):
            value = dice.roll_dice_expr("1d6", rng)
            assert 1 <= value <= 6

    def test_modifier(self) -> None:
        rng = random.Random(7)
        expected = random.Random(7).randint(1, 4) + 2
        assert dice.roll_dice_expr("1d4+2", rng) == expected

    @pytest.mark.parametrize("bad", ["", "d6", "1d", "abc", "1d6+1d4", "0d6", "1d1", "999d999999"])
    def test_invalid_raises(self, bad: str) -> None:
        with pytest.raises(ValueError):
            dice.roll_dice_expr(bad, random.Random(1))


class TestEvaluateCheck:
    """COC7 成功等级边界。目标值 45（<50）与 60（>=50）覆盖大失败的两种口径。"""

    @pytest.mark.parametrize(
        ("rolled", "target", "level"),
        [
            (1, 45, dice.LEVEL_CRITICAL),  # 01 恒为大成功
            (9, 45, dice.LEVEL_EXTREME),  # <= 45//5
            (22, 45, dice.LEVEL_HARD),  # <= 45//2
            (45, 45, dice.LEVEL_REGULAR),  # 恰好等于目标
            (46, 45, dice.LEVEL_FAIL),
            (96, 45, dice.LEVEL_FUMBLE),  # 技能<50：96-100 大失败
            (95, 45, dice.LEVEL_FAIL),  # 95 还不是
            (96, 60, dice.LEVEL_FAIL),  # 技能>=50：96 只是失败
            (100, 60, dice.LEVEL_FUMBLE),  # 100 恒为大失败
        ],
    )
    def test_levels(self, rolled: int, target: int, level: str) -> None:
        assert dice.evaluate_check(rolled, target).level == level

    def test_succeeded_property(self) -> None:
        assert dice.evaluate_check(30, 45).succeeded
        assert not dice.evaluate_check(50, 45).succeeded
