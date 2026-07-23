"""掷骰与 COC7 成功等级判定（keeper agent 实验）。

纯函数 + 显式传入 `random.Random`：掷骰结果必须可复现才能单测（固定 seed），
也才有资格叫"服务端权威掷骰"——骰子由这里掷，agent（LLM）只消费结果，
改不了点数。
"""

import random
import re
from dataclasses import dataclass

# COC7 成功等级（由高到低）。大成功/大失败按 7 版规则：
# 01 恒为大成功；技能<50 时 96-100 大失败，技能>=50 时仅 100 大失败。
LEVEL_CRITICAL = "大成功"
LEVEL_EXTREME = "极难成功"
LEVEL_HARD = "困难成功"
LEVEL_REGULAR = "成功"
LEVEL_FAIL = "失败"
LEVEL_FUMBLE = "大失败"

_DICE_RE = re.compile(r"^(\d+)[dD](\d+)([+-]\d+)?$")


def roll_d100(rng: random.Random) -> int:
    return rng.randint(1, 100)


def roll_dice_expr(expr: str, rng: random.Random) -> int:
    """求值骰子表达式：`"1d6"`、`"2d6+3"`、或纯数字 `"0"`/`"5"`。

    San 损失写法（如 `0/1d6`）不在这里处理——那是"成功/失败各用哪个表达式"
    的一对，由调用方拆开分别传入。非法表达式抛 ValueError（agent 传烂参数时
    经 SDK 的 failure_error_function 反馈给模型重试，不炸进程）。
    """
    text = expr.strip()
    if re.fullmatch(r"\d+", text):
        return int(text)
    match = _DICE_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"无法解析的骰子表达式: {expr!r}")
    count, sides, modifier = int(match.group(1)), int(match.group(2)), match.group(3)
    if count < 1 or count > 100 or sides < 2 or sides > 1000:
        raise ValueError(f"骰子表达式超出合理范围: {expr!r}")
    total = sum(rng.randint(1, sides) for _ in range(count))
    return total + (int(modifier) if modifier else 0)


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    rolled: int
    target: int
    level: str

    @property
    def succeeded(self) -> bool:
        return self.level in (LEVEL_CRITICAL, LEVEL_EXTREME, LEVEL_HARD, LEVEL_REGULAR)


def evaluate_check(rolled: int, target: int) -> CheckOutcome:
    """按 COC7 判定一次 d100 检定的成功等级。

    `target` 是技能/属性的完整值；困难=一半、极难=五分之一由这里换算——
    工具层报告完整等级，"这次检定要求什么难度"由 agent 对照模组要求解释。
    """
    if rolled == 1:
        level = LEVEL_CRITICAL
    elif (target < 50 and rolled >= 96) or rolled == 100:
        level = LEVEL_FUMBLE
    elif rolled <= target // 5:
        level = LEVEL_EXTREME
    elif rolled <= target // 2:
        level = LEVEL_HARD
    elif rolled <= target:
        level = LEVEL_REGULAR
    else:
        level = LEVEL_FAIL
    return CheckOutcome(rolled=rolled, target=target, level=level)
