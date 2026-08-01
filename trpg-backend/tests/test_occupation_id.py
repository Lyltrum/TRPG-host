"""职业用 id 定位，不用名字（exec/22）。

职业名**不唯一**：规则表里有 6 组同名不同项的职业（律师 ×2、私家侦探 ×2、
工匠 ×2、艺术家 ×2、艺人 ×2、科学家 ×2），信用区间不同，其中三组连技能点
公式都不同。此前角色卡只存职业名、校验按名字查回第一个匹配，于是：

1. **合法的卡被判非法**：选了第二个律师（信用 `[20,80]`）、把信用填成 25，
   校验按第一个律师（`[30,80]`）判 → CREDIT_OUT_OF_RANGE；
2. **预算算错但不报错**（更阴）：公式不同的那三组，职业技能点预算会算成
   另一个数，玩家看不到任何提示。

又一次"用自由文本当标识符"，与 exec/17 同族。
"""

import collections

import pytest

from app.core.coc7_content import build_coc7_ruleset
from app.core.coc7_rules import (
    GENERATION_POINT_BUY,
    find_occupation_by_id,
    find_occupation_by_name,
    validate_character_with_occupation,
)

_RULESET = build_coc7_ruleset()


def _duplicate_names() -> dict[str, list]:
    counter = collections.Counter(o.name for o in _RULESET.occupations)
    return {
        name: [o for o in _RULESET.occupations if o.name == name]
        for name, n in counter.items()
        if n > 1
    }


def test_the_ambiguity_this_is_about_actually_exists() -> None:
    """先钉住前提：职业表里**确实**有同名不同项的职业。

    哪天数据被去重了，这条会红——那时该回来重新评估 exec/22 还有没有必要，
    而不是让一堆绕开歧义的代码继续留着没人知道为什么。
    """
    dupes = _duplicate_names()
    assert dupes, "职业表已经没有同名项了，exec/22 的前提消失"
    # 至少有一组连信用区间都不同——这正是能把合法卡判非法的那种
    assert any(
        len({(o.credit_min, o.credit_max) for o in variants}) > 1 for variants in dupes.values()
    )


def test_name_lookup_cannot_tell_the_variants_apart() -> None:
    """按名字查只能拿回第一个——这就是信息丢失的那一步。"""
    dupes = _duplicate_names()
    name, variants = next(iter(dupes.items()))
    found, not_found = find_occupation_by_name(_RULESET.occupations, name)
    assert not not_found
    assert found is not None
    assert found.id == variants[0].id
    assert any(v.id != found.id for v in variants), "这组同名项的 id 应该不同"


def test_id_lookup_gets_the_exact_variant() -> None:
    dupes = _duplicate_names()
    _name, variants = next(iter(dupes.items()))
    for variant in variants:
        found, not_found = find_occupation_by_id(_RULESET.occupations, variant.id)
        assert not not_found
        assert found is not None and found.id == variant.id


def test_card_legal_for_one_variant_is_judged_by_that_variant() -> None:
    """🔴 核心回归：信用值取"第二个变体的下限"，按 id 校验必须合法。

    按名字校验会拿第一个变体的区间去判——那正是玩家会遇到的"我明明按规则
    填的，它说我不合法"。
    """
    dupes = {
        name: variants
        for name, variants in _duplicate_names().items()
        if len({v.credit_min for v in variants}) > 1
    }
    assert dupes, "没有信用下限不同的同名组，这条用例失去意义"
    name, variants = next(iter(dupes.items()))
    # 挑下限更低的那个变体，用它的下限当信用值
    variant = min(variants, key=lambda o: o.credit_min)
    other = max(variants, key=lambda o: o.credit_min)
    assert variant.credit_min < other.credit_min

    attributes = {
        "STR": 60,
        "CON": 60,
        "SIZ": 60,
        "DEX": 60,
        "APP": 60,
        "INT": 60,
        "POW": 60,
        "EDU": 60,
        "LUCK": 60,
    }
    skills = {"credit-rating": variant.credit_min}

    by_id = validate_character_with_occupation(
        _RULESET,
        attributes=attributes,
        occupation=variant,
        skills=skills,
        generation_method=GENERATION_POINT_BUY,
    )
    assert not by_id, f"按 id 校验应当合法：{[i.code for i in by_id]}"

    # 反向确认这条用例真的走进了被测差异：拿另一个同名变体去判会失败
    by_other = validate_character_with_occupation(
        _RULESET,
        attributes=attributes,
        occupation=other,
        skills=skills,
        generation_method=GENERATION_POINT_BUY,
    )
    assert any(i.code == "CREDIT_OUT_OF_RANGE" for i in by_other)


def test_no_occupation_is_not_the_same_as_occupation_not_found() -> None:
    """🔴 "没选职业"（合法）与"选了但查不到"（非法）是两件事。

    第一版 `validate_character_with_occupation` 把 `occupation_not_found`
    写成 `occupation is None`，于是所有未选职业的卡都被误判成
    OCCUPATION_NOT_FOUND——三条既有用例当场转红才发现。
    """
    attributes = dict.fromkeys(["STR", "CON", "SIZ", "DEX", "APP", "INT", "POW", "EDU", "LUCK"], 55)
    no_occupation = validate_character_with_occupation(
        _RULESET, attributes=attributes, occupation=None, skills={}
    )
    assert not any(i.code == "OCCUPATION_NOT_FOUND" for i in no_occupation)

    lookup_failed = validate_character_with_occupation(
        _RULESET, attributes=attributes, occupation=None, skills={}, occupation_not_found=True
    )
    assert any(i.code == "OCCUPATION_NOT_FOUND" for i in lookup_failed)


@pytest.mark.parametrize("occupation", _RULESET.occupations[:40])
def test_every_occupation_is_reachable_by_id(occupation) -> None:
    found, not_found = find_occupation_by_id(_RULESET.occupations, occupation.id)
    assert not not_found
    assert found is not None and found.id == occupation.id
