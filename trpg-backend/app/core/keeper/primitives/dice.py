"""掷骰与 COC7 成功等级判定（keeper agent 实验）。

纯函数 + 显式传入 `random.Random`：掷骰结果必须可复现才能单测（固定 seed），
也才有资格叫"服务端权威掷骰"——骰子由这里掷，agent（LLM）只消费结果，
改不了点数。
"""

import random
import re
from dataclasses import dataclass

from app.core.coc7.rules import SUCCESS_TIER_DIVISORS

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

    🔴 除数来自 `coc7.rules.SUCCESS_TIERS`，跟角色卡上那三格展示的是**同一份**
    ——两处各写一遍的话，改规则时必漏一处。
    """
    if rolled == 1:
        level = LEVEL_CRITICAL
    elif (target < 50 and rolled >= 96) or rolled == 100:
        level = LEVEL_FUMBLE
    elif rolled <= target // SUCCESS_TIER_DIVISORS["extreme"]:
        level = LEVEL_EXTREME
    elif rolled <= target // SUCCESS_TIER_DIVISORS["hard"]:
        level = LEVEL_HARD
    elif rolled <= target:
        level = LEVEL_REGULAR
    else:
        level = LEVEL_FAIL
    return CheckOutcome(rolled=rolled, target=target, level=level)


#: 成功等级由高到低。对抗检定比的就是它（COC7 Opposed Roll）。
_LEVEL_RANK = {
    LEVEL_CRITICAL: 5,
    LEVEL_EXTREME: 4,
    LEVEL_HARD: 3,
    LEVEL_REGULAR: 2,
    LEVEL_FAIL: 1,
    LEVEL_FUMBLE: 0,
}


def resolve_opposed(actor: CheckOutcome, opponent: CheckOutcome) -> bool:
    """COC7 对抗检定：主动方赢了没有（exec/19 #38）。

    规则顺序：
    0. **主动方自己没掷过 → 一定没赢。** 对抗掷骰里失败的一方不可能获胜；
       双方都失败就是僵局（维持现状），没有赢家。
    1. 双方都成功 → 成功等级高者胜；
    2. 等级相同 → 目标值（技能/属性值）高者胜；
    3. 仍相同 → **判主动方失败**。

    第 3 条是本项目的裁定，不是规则书原文（书上说重掷或由 KP 裁定）：真人在
    等回复，重掷要多一次往返；而"平局时维持现状"对防守方有利，正是对抗检定
    的常见默认。写死在代码里，不交给模型即兴。

    🔴 第 0 条是**试玩实测补上的**（2026-07-31，exec/19 #43）。初版只比成功
    等级的序号，于是 `失败(rank 1) > 大失败(rank 0)`、以及两边都失败时"技能值
    高者胜"，都会判出一个赢家。实际发生过：玩家力量 63/60 失败、对手 62/50
    也失败，系统却记 `won=True`，而给叙事的文本是「63/60（失败） vs 62/50
    （失败） → 胜」——自相矛盾。**叙事模型选择按"失败"写，它比代码判对了。**
    """
    if not actor.succeeded:
        return False
    if not opponent.succeeded:
        return True
    actor_rank, opponent_rank = _LEVEL_RANK[actor.level], _LEVEL_RANK[opponent.level]
    if actor_rank != opponent_rank:
        return actor_rank > opponent_rank
    return actor.target > opponent.target


#: 对抗检定的三种结果。**`won: bool` 装不下它**——那正是 2026-08-14 实测里
#: 出错的地方：玩家格斗 39/25 失败、米-戈 56/40 也失败，`resolve_opposed`
#: 判 `won=False`（**判对了**），而给叙事的文本写的是「凌铭辉负」。
#: 「负」被读成"对方赢了"，于是叙事让米-戈成功抓住玩家、造成 2 点伤害
#: ——**双方都失败在 COC 里是僵持，防守方不白赚一次攻击。**
#:
#: 又一处「一份数据扮演两个角色」：`won=False` 同时表达"我输了"和"没人赢"。
VERDICT_WIN = "胜"
VERDICT_LOSE = "负"
VERDICT_STALEMATE = "僵持"


def opposed_verdict(actor: CheckOutcome, opponent: CheckOutcome) -> str:
    """对抗检定的三态结论。`resolve_opposed` 的表达补全，不改它的判定。

    两边都没掷过 → 僵持（维持现状）；其余按 `resolve_opposed` 的胜负。
    """
    if not actor.succeeded and not opponent.succeeded:
        return VERDICT_STALEMATE
    return VERDICT_WIN if resolve_opposed(actor, opponent) else VERDICT_LOSE


def is_success(level: str) -> bool:
    """这次检定算不算成功。失败/大失败以外都算（含大成功与各档难度成功）。"""
    return level not in (LEVEL_FAIL, LEVEL_FUMBLE)
