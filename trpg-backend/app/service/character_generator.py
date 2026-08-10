"""合法角色卡生成器：**中立**，不属于任何一条调用路径。

两个调用方：
- 玩家的「一键生成」（`service/character.py::quick_build_character`）——真人实测
  反馈：八步向导对新人不友好，光"职业技能点该怎么分"就足以劝退；
- AI 队友的建卡（`service/ai_player.py`）。

## 🔴 为什么单独一个模块（2026-08-10）

它原本住在 `ai_player.py` 里，玩家路径 `import` 它。于是玩家继承的不只是代码，
还有**那个模块里按"AI 补位"定的每一个产品判断**——最明显的三条：兴趣点故意
不花（"AI 不该比真人玩家更强"，可玩家的卡就是它生成的，这条理由自相矛盾）、
固定 30 岁、`generation_method` 标成点数购买。用户当场指出："一键生成走的应该是
和真人自己加点一样的完整逻辑。"

形状是项目里已有的判据：**一份东西扮演两个角色必出结构性 bug**（先例是建卡的
「分配值 / 有效值」）。共用一个**合法卡生成器**没问题——两边都需要一张合法的卡；
错的是它住在其中一方家里，于是默认值全按那一方的需要定。

**本来就该由调用方决定的东西一律做成参数**（年龄）；**两边都一样的东西不给
开关**（点数必须花完——没有哪个调用方需要一张没花完点数的卡）。

## 规则函数一个都不自己重写

属性区间、职业技能点公式、信用评级分账全部复用 `coc7/rules`。

## 🔴 生成后仍然跑 `validate_character_with_occupation`

人类建卡时那次校验是**防客户端伪造**；这里数据是我们自己造的，没人可骗——
这次是**防我们自己的生成器写出不合法的卡**。纯函数、几乎不花时间，CI 里守门。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from app.core.coc7.content import build_coc7_ruleset
from app.core.coc7.rules import (
    GENERATION_POINT_BUY,
    NON_ALLOCATABLE_SKILL_IDS,
    SKILL_CAP,
    evaluate_skill_base,
    evaluate_skill_points_formula,
    validate_character_with_occupation,
)
from app.dto.game import OccupationSpec, RulesetRead, SkillSpec

#: 幸运不参与点数购买，只能掷（`_validate_attributes` 对它走宽松区间）。
_LUCK_KEY = "LUCK"

#: 兴趣点摊给几项技能。全摊到所有技能上会出一张"每项 +2"的糊卡；只摊一两项
#: 又会顶上限白丢点数。
_INTEREST_SKILL_COUNT = 6

#: 默认年龄。COC7 的年龄修正在 20–39 岁区间内为零，于是「分配值」与「有效值」
#: 两份属性完全相同——生成器因此不需要维护年龄修正那套双份记账
#: （见 character-build-migration round4 方案 A）。**调用方要给别的年龄，
#: 得自己确认那套记账**。
DEFAULT_AGE_RANGE = (20, 39)


class GeneratedCharacterInvalidError(RuntimeError):
    """生成器造出了不合法的卡——**我们自己的 bug**，不是用户输入问题。

    单独一个异常类型而不是复用 `CharacterInvalidError`：那个是 400（客户端的卡
    有问题），这个该是 500（我们的生成逻辑有问题），语义不能混。
    """


def _allocate_attributes(ruleset: RulesetRead, rng: random.Random) -> dict[str, int]:
    """按点数购买法生成一套合法属性。

    **不掷骰**：走这条路的人（或 AI）没在做"要不要重掷"的决定，掷骰只会带来
    一张运气很差、他又没得选的卡。做法是把预算平均分给参与点数购买的属性，
    再做几次「一加一减」的等量互换制造一点个体差异——总和恒定，永远不超预算。

    互换幅度锁在 ±10 且逐次检查区间，任何一次越界就跳过那次互换（而不是夹紧
    到边界）——夹紧会悄悄改变总和。
    """
    point_buy = ruleset.attribute_point_buy
    pool_keys = [a.key for a in ruleset.attributes if a.key != _LUCK_KEY]
    if point_buy is None:
        # 自定义系统没配点数购买约束 → 没有预算可分，全部取默认值。
        # 不硬套 COC7 的数字（issue #112 的原则）。
        values = dict.fromkeys(pool_keys, 50)
    else:
        share = point_buy.budget // len(pool_keys)
        values = dict.fromkeys(pool_keys, share)
        leftover = point_buy.budget - share * len(pool_keys)
        if leftover and pool_keys:
            values[pool_keys[0]] += leftover
        for _ in range(6):
            a, b = rng.sample(pool_keys, 2)
            delta = rng.choice((5, 10))
            if (
                point_buy.min_value <= values[a] - delta
                and values[b] + delta <= point_buy.max_value
            ):
                values[a] -= delta
                values[b] += delta
    # 幸运不参与点数购买，只能掷（3d6×5，COC7 规则）
    values[_LUCK_KEY] = sum(rng.randint(1, 6) for _ in range(3)) * 5
    return values


def _pick_slot_skills(
    occupation: OccupationSpec, ruleset: RulesetRead, rng: random.Random
) -> list[str]:
    """给职业的自选槽各挑一个技能 id。

    `candidate_skill_ids` 为 None 的槽（"任意一项其他个人或时代特长"）从技能
    表里随机挑一个非职业、非禁选的技能。挑重了就跳过——槽位少挑一个只是少花
    点数，不会造成不合法。
    """
    picked: list[str] = []
    taken = set(occupation.skill_ids)
    for slot in occupation.choice_slots:
        candidates = slot.candidate_skill_ids or [
            s.id
            for s in ruleset.skills
            if s.id not in taken
            and s.id not in NON_ALLOCATABLE_SKILL_IDS
            and s.id != "credit-rating"
        ]
        available = [c for c in candidates if c not in taken]
        for _ in range(min(slot.count, len(available))):
            choice = rng.choice(available)
            available.remove(choice)
            taken.add(choice)
            picked.append(choice)
    return picked


def _spend_points(
    points: int,
    targets: list[str],
    skills: dict[str, int],
    by_id: dict[str, SkillSpec],
    attributes: dict[str, int],
) -> int:
    """把 `points` 点摊到 `targets` 上，返回**真正花掉**的点数。

    🔴 摊完还要把余数继续摊，直到点数用光或所有目标都顶到 `SKILL_CAP`——
    一轮整除除不尽的余数、以及撞了上限退回来的点数，都必须再找地方花掉。
    没有这个循环就总会剩几点（旧版每次剩 0–8 点），而**新人玩家不会接受
    带着没花完的点数开局**。
    """
    room = {}
    for skill_id in targets:
        spec = by_id.get(skill_id)
        if spec is None:
            continue  # 职业表引用了技能表里没有的 id —— 跳过，别造非法数据
        skills.setdefault(skill_id, evaluate_skill_base(spec.base, attributes))
        room[skill_id] = max(0, SKILL_CAP - skills[skill_id])

    spent = 0
    remaining = points
    while remaining > 0:
        live = [sid for sid in room if room[sid] > 0]
        if not live:
            break  # 全顶到上限了：剩下的点数无处可花，这不是记账错误
        share = max(1, remaining // len(live))
        for skill_id in live:
            give = min(share, room[skill_id], remaining)
            if give <= 0:
                continue
            skills[skill_id] += give
            room[skill_id] -= give
            remaining -= give
            spent += give
            if remaining <= 0:
                break
    return spent


def _allocate_skills(
    ruleset: RulesetRead,
    occupation: OccupationSpec,
    attributes: dict[str, int],
    rng: random.Random,
) -> dict[str, int]:
    """把技能点分配成一份 `skills` 字典（存的是**技能总值**，不是加点数）。

    记账口径完全按 `coc7_rules._compute`：
    - 信用评级取 `credit_min`——那部分点数按 Chaosium 官方裁定算职业点负担，
      取下限就是"不额外花钱"的选择；
    - 剩下的职业点摊给固定本职技能 + 自选槽技能；
    - **兴趣点摊给一批非职业技能**，也要花完。

    🔴 **两个池子都必须花光**（2026-08-10 用户指出）。旧版刻意不花兴趣点，
    理由写的是"让 AI 的卡朴素一点，它不该比真人玩家更强"——**共用之后这条理由
    自相矛盾**：玩家的「一键生成」走的就是这个生成器，被比较的"真人玩家"就是
    同一张卡，于是为了不让 AI 比玩家强，把玩家自己削弱了 80–150 点。
    AI 该弱在**决策**上（`exec/21` 的有限视角），**削弱不该藏在数值里**——
    数值上砍它，玩家看到的是"AI 技能烂"而不是"AI 判断差"。
    """
    by_id = {s.id: s for s in ruleset.skills}
    occupation_budget = evaluate_skill_points_formula(occupation.skill_points_formula, attributes)
    skills: dict[str, int] = {"credit-rating": occupation.credit_min}

    occupation_targets = [
        t
        for t in list(occupation.skill_ids) + _pick_slot_skills(occupation, ruleset, rng)
        if t not in NON_ALLOCATABLE_SKILL_IDS
    ]
    _spend_points(
        max(0, occupation_budget - occupation.credit_min),
        occupation_targets,
        skills,
        by_id,
        attributes,
    )

    # 兴趣点只能花在**非职业**技能上——花在本职技能上会被 `_compute` 记进职业池，
    # 于是职业池超支、溢出又转回兴趣池，两边都对不上账。
    interest_budget = attributes.get("INT", 0) * 2
    chosen = set(occupation_targets) | {"credit-rating"}
    candidates = [
        s.id for s in ruleset.skills if s.id not in chosen and s.id not in NON_ALLOCATABLE_SKILL_IDS
    ]
    if interest_budget > 0 and candidates:
        # 挑几项当"个人爱好"：全摊到所有技能上会摊出一张每项 +2 的糊卡，
        # 只摊一两项又会顶上限白丢点数。
        interest_targets = rng.sample(candidates, k=min(_INTEREST_SKILL_COUNT, len(candidates)))
        _spend_points(interest_budget, interest_targets, skills, by_id, attributes)
    return skills


@dataclass(frozen=True, slots=True)
class RolledSheet:
    """一张随机生成的、**规则上合法**的角色卡数据（未落库）。"""

    occupation: OccupationSpec
    attributes: dict[str, int]
    skills: dict[str, int]
    age: int


def roll_character_sheet(
    *, occupation_name: str | None = None, seed: int | None = None, age: int | None = None
) -> RolledSheet:
    """随机生成一张合法的 COC7 角色卡数据。

    `age` 不传就在 `DEFAULT_AGE_RANGE` 里随机——**这是调用方的决定，不是生成器的**
    （见模块 docstring：AI 此前固定 30 岁是"AI 的年龄不影响玩法"，那条判断对
    真人玩家不成立，于是每个用一键生成的新人都是 30 岁）。

    🔴 自检的目的是防**我们自己的生成器**写出不合法的卡，不是防伪造。
    🔴 按**对象**校验，不按名字：职业表里有 6 组同名不同项的职业（律师 ×2、
    私家侦探 ×2、工匠 ×2…），信用区间乃至技能点公式都不同。按名字查只能拿到
    第一个匹配——我们手里明明有确切的那一个（exec/22）。
    """
    rng = random.Random(seed)
    ruleset = build_coc7_ruleset()
    if occupation_name is None:
        occupation = rng.choice(ruleset.occupations)
    else:
        occupation = next((o for o in ruleset.occupations if o.name == occupation_name), None)
        if occupation is None:
            raise ValueError(f"没有这个职业：{occupation_name}")

    attributes = _allocate_attributes(ruleset, rng)
    skills = _allocate_skills(ruleset, occupation, attributes, rng)

    issues = validate_character_with_occupation(
        ruleset,
        attributes=attributes,
        occupation=occupation,
        skills=skills,
        generation_method=GENERATION_POINT_BUY,
    )
    if issues:
        raise GeneratedCharacterInvalidError(
            "角色卡生成器产出了不合法的卡："
            + "；".join(f"{i.code}@{i.field}:{i.message}" for i in issues)
        )
    return RolledSheet(
        occupation=occupation,
        attributes=attributes,
        skills=skills,
        age=age if age is not None else rng.randint(*DEFAULT_AGE_RANGE),
    )
