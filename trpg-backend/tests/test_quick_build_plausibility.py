"""一键建卡随机塞给玩家的技能，得是**这张卡上说得通**的。

## 🔴 起点是真机第一张卡

2026-08-17 真机跑《复足》（芝加哥现代背景），一键建出来的「工匠」带着
`驾驶：航天器 31`、`格斗：链锯`、`格斗：鞭`。生成器本身没错——它的注释写得很
清楚，只保证「规则上合法」。但一键建卡的用途是"零基础玩家立刻拿到一张能玩
的卡"，而这样一张卡会让玩家当场觉得系统不懂 COC。

## 不拿一张卡当通例

只看那一张的话，我会去列一张「链锯/鞭/航天器」的黑名单。量了 40 张之后
结论不一样：
  - `驾驶：航天器` 出现在 **10%** 的卡上（不是偶发）
  - 占位技能（外语①②③、艺术与手艺①②③）约占 13%，给了数值却没人知道是哪门
  - combat 占 18%，最常见的是「射击：冲锋枪」

前两类有客观判据（超年代 / 内容待指定），这个文件守的就是这两类。
"""

from __future__ import annotations

import collections

import pytest

from app.service.character_generator import (
    _OUT_OF_ERA_SKILL_IDS,
    _unsuitable_for_random_pick,
    roll_character_sheet,
)

#: 抽这么多张来看分布。单张卡只够提出怀疑，不够下判断。
_SAMPLE = 40


def _randomly_added_skills(sheet, base_by_id: dict[str, object]) -> list[str]:
    """这张卡上「被随机加过点的非职业技能」。

    职业技能是职业作者指定的，不在考察范围内——本文件守的是**随机取材**。
    """
    professional = set(sheet.occupation.skill_ids)
    out = []
    for skill_id, value in (sheet.skills or {}).items():
        if skill_id in professional or skill_id == "credit-rating":
            continue
        base = base_by_id.get(skill_id)
        if isinstance(base, int) and value <= base:
            continue  # 没加过点
        out.append(skill_id)
    return out


@pytest.fixture(scope="module")
def base_by_id() -> dict[str, object]:
    from app.core.coc7.content import COC7_SKILLS

    return {s.id: s.base for s in COC7_SKILLS}


def test_no_sheet_gets_a_skill_from_outside_the_era(base_by_id) -> None:
    """🔴 `驾驶：航天器` 不许出现在一键建卡的产出里。

    修之前实测 40 张里有 4 张带着它。
    """
    offenders: list[tuple[int, str]] = []
    for seed in range(_SAMPLE):
        sheet = roll_character_sheet(seed=seed)
        for skill_id in _randomly_added_skills(sheet, base_by_id):
            if skill_id in _OUT_OF_ERA_SKILL_IDS:
                offenders.append((seed, skill_id))
    assert not offenders, f"一键建卡随机塞进了超年代技能：{offenders}"


def test_no_sheet_gets_a_placeholder_skill_by_random_interest(base_by_id) -> None:
    """占位技能（`外语③`）不该来自**兴趣点**的随机取材。

    ⚠️ 它仍然可能出现在卡上——职业的自选槽如果**写明**了候选就照样选得到，
    那是职业作者的选择，不是随机。所以这里断言的是"兴趣点没挑它"，
    而不是"卡上没有它"，否则会把一条合法的路误判成缺陷。
    """
    from app.service.character_generator import _PLACEHOLDER_SKILL_SUFFIX

    for seed in range(_SAMPLE):
        sheet = roll_character_sheet(seed=seed)
        slot_candidates: set[str] = set()
        for slot in sheet.occupation.choice_slots:
            slot_candidates.update(slot.candidate_skill_ids or [])
        for skill_id in _randomly_added_skills(sheet, base_by_id):
            if _PLACEHOLDER_SKILL_SUFFIX.search(skill_id) and skill_id not in slot_candidates:
                raise AssertionError(
                    f"seed={seed}: 兴趣点随机挑中了占位技能 {skill_id}，"
                    "玩家会拿到一张写着「外语③ 45」却不知道是哪门语言的卡"
                )


def test_the_filter_only_touches_random_picking_not_the_skill_list() -> None:
    """判据本身：排除的是**随机分配**，不是把技能从规则里删掉。

    玩家手动建卡照样选得到 `pilot-spacecraft`（真要跑科幻变体局不拦着），
    这跟 `NON_ALLOCATABLE_SKILL_IDS`（规则明文禁止加点）是两件事。
    """
    from app.core.coc7.content import COC7_SKILLS
    from app.core.coc7.rules import NON_ALLOCATABLE_SKILL_IDS

    all_ids = {s.id for s in COC7_SKILLS}
    assert "pilot-spacecraft" in all_ids, "技能没被从规则里删掉——只是不再随机分配"
    assert not (_OUT_OF_ERA_SKILL_IDS & NON_ALLOCATABLE_SKILL_IDS), (
        "两张表混用了：一个是规则禁令，一个是随机取材的口味，合并就是「两件事共用一个开关」"
    )


def test_normal_skills_are_still_reachable(base_by_id) -> None:
    """反向：别把池子过滤空了。

    只断言"排除了坏的"而不断言"好的还在"，是把功能改坏也能全绿的经典形状。
    """
    seen: collections.Counter[str] = collections.Counter()
    for seed in range(_SAMPLE):
        seen.update(_randomly_added_skills(roll_character_sheet(seed=seed), base_by_id))
    assert len(seen) >= 20, f"随机取材的技能种类塌到了 {len(seen)} 种，池子被过滤过头了"


@pytest.mark.parametrize(
    "skill_id,expected",
    [
        ("pilot-spacecraft", True),  # 超年代
        ("language-foreign-3", True),  # 占位
        ("art-craft-2", True),  # 占位
        ("lore-1", True),  # 占位
        ("carpentry", False),  # 已经指定过内容的手艺，不是占位
        ("illusion", False),
        ("pilot-aircraft", False),  # 1920s 稀有但存在，不排除
        ("spot-hidden", False),
    ],
)
def test_the_criterion_itself(skill_id: str, expected: bool) -> None:
    assert _unsuitable_for_random_pick(skill_id) is expected
