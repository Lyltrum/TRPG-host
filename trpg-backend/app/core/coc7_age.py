"""COC7 建卡期年龄修正（守秘人规则书 · 创造调查员 / 年龄）。

移植自用户个人项目 coc-char-gen 的 `js/plugins/age.js`（数值/分档区间逐条
对照，未改动），执行位置搬到后端——随机数（EDU 改进检定的 d100/d10、幸运
双掷的 3d6）必须服务端权威生成，不能信任客户端算好的结果。

年龄分档表（15–89 岁）：

| 年龄     | EDU              | 身体 / 外貌              | 其他        |
|----------|------------------|--------------------------|-------------|
| 15–19    | 固定 −5          | STR 与 SIZ 合计 −5       | 幸运掷两次取高 |
| 20–39    | 改进检定 ×1      | —                        | —           |
| 40–49    | 改进检定 ×2      | STR+CON+DEX 共 −5，APP−5 | MOV−1       |
| 50–59    | 改进检定 ×3      | 共 −10，APP−10           | MOV−2       |
| 60–69    | 改进检定 ×4      | 共 −20，APP−15           | MOV−3       |
| 70–79    | 改进检定 ×4      | 共 −40，APP−20           | MOV−4       |
| 80–89    | 改进检定 ×4      | 共 −80，APP−25           | MOV−5       |

EDU 改进检定：d100 > 当前 EDU 则 EDU +1d10（上限 99）。属性扣减后最低为 1。
20–39 是官方合并档（20 多岁与 30 多岁规则相同），不是笔误。
"""

import random
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class AgeModifiers:
    """某个年龄档对应的全部修正参数（对应 age.js 里 `AGE_TABLE` 的一行）。"""

    label: str
    edu_checks: int
    edu_flat: int
    app_loss: int
    scd_loss: int
    str_siz_only: bool
    luck_twice: bool
    mov_penalty: int


# 表内每行是 (最小年龄, 最大年龄, 修正参数) 的闭区间。
_AGE_TABLE: list[tuple[int, int, AgeModifiers]] = [
    (
        15,
        19,
        AgeModifiers(
            label="15–19",
            edu_checks=0,
            edu_flat=-5,
            app_loss=0,
            scd_loss=5,
            str_siz_only=True,
            luck_twice=True,
            mov_penalty=0,
        ),
    ),
    (
        20,
        39,
        AgeModifiers(
            label="20–39",
            edu_checks=1,
            edu_flat=0,
            app_loss=0,
            scd_loss=0,
            str_siz_only=False,
            luck_twice=False,
            mov_penalty=0,
        ),
    ),
    (
        40,
        49,
        AgeModifiers(
            label="40–49",
            edu_checks=2,
            edu_flat=0,
            app_loss=5,
            scd_loss=5,
            str_siz_only=False,
            luck_twice=False,
            mov_penalty=1,
        ),
    ),
    (
        50,
        59,
        AgeModifiers(
            label="50–59",
            edu_checks=3,
            edu_flat=0,
            app_loss=10,
            scd_loss=10,
            str_siz_only=False,
            luck_twice=False,
            mov_penalty=2,
        ),
    ),
    (
        60,
        69,
        AgeModifiers(
            label="60–69",
            edu_checks=4,
            edu_flat=0,
            app_loss=15,
            scd_loss=20,
            str_siz_only=False,
            luck_twice=False,
            mov_penalty=3,
        ),
    ),
    (
        70,
        79,
        AgeModifiers(
            label="70–79",
            edu_checks=4,
            edu_flat=0,
            app_loss=20,
            scd_loss=40,
            str_siz_only=False,
            luck_twice=False,
            mov_penalty=4,
        ),
    ),
    (
        80,
        89,
        AgeModifiers(
            label="80–89",
            edu_checks=4,
            edu_flat=0,
            app_loss=25,
            scd_loss=80,
            str_siz_only=False,
            luck_twice=False,
            mov_penalty=5,
        ),
    ),
]

# 表外的兜底档（年龄 <15 或 >=90，规则书没有标准数据，age.js 原样保留、只是
# 提示"请与 KP 商议"，不拒绝——这里跟随同样的宽松处理）。
_UNDER_15 = AgeModifiers(
    label="<15",
    edu_checks=0,
    edu_flat=0,
    app_loss=0,
    scd_loss=0,
    str_siz_only=False,
    luck_twice=False,
    mov_penalty=0,
)


def max_total_adjustment_magnitude() -> int:
    """年龄修正理论上能让「分配值→有效值」总共偏离多少点——取全表最坏
    一档的 `EDU 改进检定总增量的上限（次数×10）+ |EDU 固定调整| + 身体
    减值 scd_loss + APP 减值 app_loss` 之和（同一档的 edu_checks 和
    edu_flat 互斥，不会同时非零，直接相加不会重复计）。这不是逐属性逐档
    精确重放（那需要另外跟踪"这一档到底能动哪几个属性"），而是一个保守
    的**总预算上限**：只要"有效值相对分配值的总偏离"不超过这个预算，
    客户端就不可能靠伪造有效值凭空多拿到规则允许范围之外的属性点，够
    堵住"全部属性怼到 99"这类粗暴伪造。"""
    rows = [m for _, _, m in _AGE_TABLE] + [_UNDER_15]
    return max(r.edu_checks * 10 + abs(r.edu_flat) + r.scd_loss + r.app_loss for r in rows)


def get_age_modifiers(age: int) -> AgeModifiers:
    """按年龄查表返回修正参数。90 岁以上沿用 80–89 档（age.js 同款处理）。"""
    if age < 15:
        return _UNDER_15
    if age >= 90:
        _, _, row = _AGE_TABLE[-1]
        return replace(row, label="90+")
    for low, high, row in _AGE_TABLE:
        if low <= age <= high:
            return row
    # 15 <= age < 90 必然落在上面某一档里，理论上不可达。
    raise ValueError(f"无法识别的年龄: {age}")


def distribute_scd_loss(
    attributes: dict[str, int], loss: int, only_str_siz: bool
) -> dict[str, int]:
    """把 SCD（身体）减值轮转分摊到相关属性，每项最低减到 1。

    `only_str_siz` 为真时只在 STR/SIZ 之间轮转（15–19 青年档），否则在
    STR/CON/DEX 之间轮转（40 岁起各档）——跟 age.js::distributeScdLoss 一致。
    """
    next_attrs = dict(attributes)
    if not loss:
        return next_attrs

    keys = ["STR", "SIZ"] if only_str_siz else ["STR", "CON", "DEX"]
    remain = loss
    while remain > 0:
        moved = False
        for key in keys:
            if remain <= 0:
                break
            if next_attrs.get(key, 0) > 1:
                next_attrs[key] -= 1
                remain -= 1
                moved = True
        if not moved:
            break
    return next_attrs


def apply_app_loss(attributes: dict[str, int], loss: int) -> dict[str, int]:
    """APP 减 `loss`，最低减到 1。"""
    next_attrs = dict(attributes)
    next_attrs["APP"] = max(1, next_attrs.get("APP", 0) - loss)
    return next_attrs


def _roll(n: int, sides: int) -> int:
    return sum(random.randint(1, sides) for _ in range(n))


def roll_edu_improvement(edu: int) -> tuple[bool, int, int, int]:
    """EDU 改进检定：服务端权威掷 d100，`roll > edu` 才算成功，成功再掷 1d10
    当增量（上限 99）。返回 `(success, roll, gain, new_edu)`。"""
    roll = _roll(1, 100)
    if roll > edu:
        gain = _roll(1, 10)
        return True, roll, gain, min(99, edu + gain)
    return False, roll, 0, edu
