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
        ("firearm-smg", True),  # 武器专精：不该是"随机个人爱好"
        ("fighting-spear", True),
        ("artillery", True),
        ("dodge", False),  # 人人都在躲
        ("fighting-brawl", False),  # 不需要受训的打架
        ("carpentry", False),  # 已经指定过内容的手艺，不是占位
        ("illusion", False),
        ("pilot-aircraft", False),  # 1920s 稀有但存在，不排除
        ("spot-hidden", False),
    ],
)
def test_the_criterion_itself(skill_id: str, expected: bool) -> None:
    from app.core.coc7.content import COC7_SKILLS

    spec = next(s for s in COC7_SKILLS if s.id == skill_id)
    assert _unsuitable_for_random_pick(spec) is expected


def test_weapon_skills_only_come_from_explicit_occupation_slots(base_by_id) -> None:
    """🔴 武器专精只许来自职业**写明了候选**的槽，不许来自随机取材。

    这条是「别把功能改坏也能全绿」的防线：光断言"combat 变少了"是不够的——
    少到 0 也算变少，而那会让军人没有枪。这里区分两种来源：
      · 职业槽（军人在步枪/手枪里选一把）→ **必须留着**
      · 随机兴趣 / 任意槽（一个工匠的"个人爱好"是冲锋枪）→ 必须挡掉

    实测（40 张）：修法之后漏出 0 项，剩下的 21 项次全部来自职业槽。
    """
    from app.core.coc7.content import COC7_SKILLS
    from app.service.character_generator import _COMBAT_SKILLS_OK_AT_RANDOM

    category_of = {s.id: s.category for s in COC7_SKILLS}
    leaked: list[tuple[int, str, str]] = []
    from_slots = 0

    for seed in range(_SAMPLE):
        sheet = roll_character_sheet(seed=seed)
        slot_candidates: set[str] = set()
        for slot in sheet.occupation.choice_slots:
            slot_candidates.update(slot.candidate_skill_ids or [])
        for skill_id in _randomly_added_skills(sheet, base_by_id):
            if category_of.get(skill_id) != "combat":
                continue
            if skill_id in _COMBAT_SKILLS_OK_AT_RANDOM:
                continue  # 闪避 / 斗殴，本来就允许
            if skill_id in slot_candidates:
                from_slots += 1
            else:
                leaked.append((seed, sheet.occupation.name, skill_id))

    assert not leaked, f"武器专精从随机取材漏了出来：{leaked[:5]}"
    assert from_slots > 0, "一张卡都没从职业槽拿到武器——过滤过头了，该有枪的职业也没枪了"


# ── 核心技能加权（2026-08-18 两局真机） ────────────────────────────

#: 分布断言用的样本量。比 `_SAMPLE` 大：这里断的是比例，不是"一次都不许出现"。
#: 种子固定 ⇒ 每次跑同一批卡，**不会因为随机而闪红**。
_WEIGHT_SAMPLE = 200

#: 「这张卡查得了案吗」的最低标准：这四项里至少点过一项。
_INVESTIGATIVE_CORE = ("spot-hidden", "listen", "library-use", "psychology")


def _pointed(sheet, skill_id: str, base_by_id: dict[str, object]) -> bool:
    base = base_by_id.get(skill_id)
    value = (sheet.skills or {}).get(skill_id, 0)
    return value > base if isinstance(base, int) else value > 0


def test_core_skills_are_favoured_by_random_interest(base_by_id) -> None:
    """🔴 兴趣点得偏向「COC 调查员十有八九会点的那几项」。

    **起点是两局真机**（2026-08-18）：守秘人反复要求侦察、聆听、追踪、话术，
    而一键建出来的卡这几项全是基础值 —— 目标值 5 或 10，第二局五次检定成功
    一次。根因是兴趣点在**全部**非职业技能里等概率 `rng.sample`，
    `乘骑`/`炮术` 和 `侦察` 一样容易被选中。

    实测（300 张）：加权前侦察 46%、加权后 69%。这里的门槛取 60%，
    卡在两者之间 —— 把权重改回 1 会当场变红。
    """
    pointed = sum(
        _pointed(roll_character_sheet(seed=seed), "spot-hidden", base_by_id)
        for seed in range(_WEIGHT_SAMPLE)
    )
    rate = pointed / _WEIGHT_SAMPLE
    assert rate >= 0.60, f"侦察只有 {rate:.0%} 的卡点过，兴趣点没有偏向核心技能"


def test_almost_every_sheet_can_actually_investigate(base_by_id) -> None:
    """一张「四项调查核心一项都没点」的卡，玩调查模组时寸步难行。

    加权前 10%，加权后 1%。门槛取 5%。
    """
    blind = [
        seed
        for seed in range(_WEIGHT_SAMPLE)
        if not any(
            _pointed(roll_character_sheet(seed=seed), sid, base_by_id)
            for sid in _INVESTIGATIVE_CORE
        )
    ]
    rate = len(blind) / _WEIGHT_SAMPLE
    assert rate <= 0.05, f"{rate:.0%} 的卡四项调查核心一项都没点（种子 {blind[:5]}…）"


def test_the_weighting_is_never_a_floor(base_by_id) -> None:
    """🔴 **是加权，不是保底**（用户 2026-08-18 拍板的那一半）。

    保底能把覆盖率钉到 100%，代价是每张一键卡长得一模一样；而一键建卡是
    **起点不是终点**（生成完还能从准备页回向导里自己调）。所以这条守的是
    反方向：**不许**有人把它"改进"成人人都有侦察。

    两个断言缺一不可：核心技能必须有卡**没有**它，非核心技能必须有卡**有**它。

    🔴 **验它的时候别拿"把权重开到很大"当变异体**：核心表有十来项在抢
    `_INTEREST_SKILL_COUNT` 个名额，权重再大侦察也有一半几率落选 —— 那不是保底。
    真正的变异体是**代码硬塞**（`interest_targets[-1] = "spot-hidden"`），
    那个当场让这条红。
    """
    sheets = [roll_character_sheet(seed=seed) for seed in range(_WEIGHT_SAMPLE)]

    without_core = [s for s in sheets if not _pointed(s, "spot-hidden", base_by_id)]
    assert without_core, "每张卡都有侦察 —— 这是保底，不是加权"

    # 对照：一项**不在**核心表里的普通技能仍然抽得到，说明池子没被核心表垄断
    with_plain = [s for s in sheets if _pointed(s, "ride", base_by_id)]
    assert with_plain, "非核心技能一张卡都没抽到 —— 加权把池子压死了"
