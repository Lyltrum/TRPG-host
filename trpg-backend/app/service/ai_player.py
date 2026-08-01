"""AI 玩家的座位与角色卡（exec/21 第二层）。

## 为什么不走建卡流程

建卡的 HTTP 三步（POST draft → PATCH → POST complete）是**前端向导的形状**，
不是规则的形状：规则校验本来就在 service 层（`complete_character` 调
`coc7_rules.validate_character` + `compute_derived_stats`）。为了造一行数据走
三次往返 + 一次向导状态机，纯属绕路。

这里直接按规则算好、一次落库。但**规则函数一个都不自己重写**——属性区间、
职业技能点公式、信用评级分账、衍生值全部复用 `coc7_rules`。

## 🔴 生成后仍然要跑 `validate_character`，但目的变了

人类建卡时那次校验是**防客户端伪造**（客户端可以传一份全 99 的属性）。
这里数据是我们自己生成的，没人可骗——这次是**防我们自己的生成器写出不合法
的卡**。纯函数、几乎不花时间，且能在 CI 里守门。

现实教训：定性试玩脚本此前直接 PATCH 一堆技能数字进去，一个数都没过职业
技能点校验。**直接塞数据的代价不是"不安全"，是你不知道手上这张卡合不合法**，
于是拿它测出来的检定成功率也说不清。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.coc7_content import build_coc7_ruleset
from app.core.coc7_rules import (
    GENERATION_POINT_BUY,
    NON_ALLOCATABLE_SKILL_IDS,
    SKILL_CAP,
    compute_derived_stats,
    evaluate_skill_base,
    evaluate_skill_points_formula,
    validate_character_with_occupation,
)
from app.dto.game import OccupationSpec, RulesetRead
from app.models.room import Character, Player, Room

#: AI 调查员固定 30 岁。COC7 的年龄修正在 20–39 岁区间内为零，于是「分配值」
#: 与「有效值」两份属性完全相同——不需要为 AI 玩家维护年龄修正那套双份记账
#: （见 character-build-migration round4 方案 A）。这是有意的简化，不是遗漏：
#: AI 玩家的年龄目前不影响任何玩法。
_AI_AGE = 30

#: 参与点数购买的属性之外，幸运只能掷（`_validate_attributes` 对它走宽松区间）。
_LUCK_KEY = "LUCK"


class AiCharacterInvalidError(RuntimeError):
    """生成器造出了不合法的卡——**我们自己的 bug**，不是用户输入问题。

    单独一个异常类型而不是复用 `CharacterInvalidError`：那个是 400（客户端
    的卡有问题），这个该是 500（我们的生成逻辑有问题），语义不能混。
    """


def _allocate_attributes(ruleset: RulesetRead, rng: random.Random) -> dict[str, int]:
    """按点数购买法生成一套合法属性。

    **不掷骰**：AI 玩家不需要运气差异，它的存在意义是补位而不是当主角。
    做法是把预算平均分给参与点数购买的属性，再做几次「一加一减」的等量互换
    制造一点个体差异——总和恒定，所以永远不会超预算。

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


def _allocate_skills(
    ruleset: RulesetRead,
    occupation: OccupationSpec,
    attributes: dict[str, int],
    rng: random.Random,
) -> dict[str, int]:
    """把职业技能点分配成一份 `skills` 字典（存的是**技能总值**，不是加点数）。

    记账口径完全按 `coc7_rules._compute`：
    - 信用评级取 `credit_min`——那部分点数按 Chaosium 官方裁定算职业点负担，
      取下限就是"不额外花钱"的选择；
    - 剩下的职业点平均分给固定本职技能 + 自选槽技能；
    - **兴趣点一分不花**。合法（兴趣点没有"必须花完"的规则），而且让 AI 的卡
      在数值上明显朴素一点——它是补位的，不该比真人玩家更强。
    """
    occupation_budget = evaluate_skill_points_formula(occupation.skill_points_formula, attributes)
    skills: dict[str, int] = {"credit-rating": occupation.credit_min}
    spent_on_credit = occupation.credit_min

    targets = list(occupation.skill_ids) + _pick_slot_skills(occupation, ruleset, rng)
    targets = [t for t in targets if t not in NON_ALLOCATABLE_SKILL_IDS]
    remaining = max(0, occupation_budget - spent_on_credit)
    if not targets or remaining <= 0:
        return skills

    by_id = {s.id: s for s in ruleset.skills}
    share = remaining // len(targets)
    for skill_id in targets:
        spec = by_id.get(skill_id)
        if spec is None:
            continue  # 职业表引用了技能表里没有的 id —— 跳过，别造非法数据
        base = evaluate_skill_base(spec.base, attributes)
        skills[skill_id] = min(base + share, SKILL_CAP)
    return skills


@dataclass(frozen=True, slots=True)
class RolledSheet:
    """一张随机生成的、**规则上合法**的角色卡数据（未落库）。"""

    occupation: OccupationSpec
    attributes: dict[str, int]
    skills: dict[str, int]
    age: int = _AI_AGE


def roll_character_sheet(
    *, occupation_name: str | None = None, seed: int | None = None
) -> RolledSheet:
    """随机生成一张合法的 COC7 角色卡数据。

    两个调用方共用：AI 队友（本模块）、玩家的「一键生成」（`service/character.py`
    的 quick_build，给零基础玩家用——真人实测反馈：整套向导对新人不友好）。
    共用一份是有意的：**同一个生成器意味着 AI 队友和新手卡强弱同源**，不会出现
    "AI 的卡莫名其妙比我好"。

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
        raise AiCharacterInvalidError(
            "角色卡生成器产出了不合法的卡："
            + "；".join(f"{i.code}@{i.field}:{i.message}" for i in issues)
        )
    return RolledSheet(occupation=occupation, attributes=attributes, skills=skills)


#: AI 调查员的默认名字池。真人玩家一眼要能认出"这是个 AI 队友"，所以不取
#: 会跟真人混淆的普通人名。
_DEFAULT_NICKNAMES = ("阿铁", "阿铜", "阿锡", "阿锌")


async def add_ai_player_to_room(
    db: AsyncSession,
    room_id: str,
    reconnect_token: str | None,
    *,
    nickname: str | None = None,
    occupation_name: str | None = None,
    seed: int | None = None,
) -> Player:
    """房主给房间加一个 AI 队友（API 入口，带鉴权与人数/阶段约束）。

    只允许在**开局之前**加（Lobby / Building）：开局后半途插入一个成员会打乱
    位置分组与叙事名单，那是另一件事，不在本期范围。
    """
    from app.service.room import RoomConflictError, _require_host, find_room_by_id

    room = await find_room_by_id(db, room_id)
    await _require_host(db, room, reconnect_token)
    if room.phase not in ("Lobby", "Building"):
        raise RoomConflictError("只有开局前可以加 AI 队友")

    rows = await db.execute(select(Player).where(Player.room_id == room_id))
    existing = list(rows.scalars())
    if len(existing) >= room.max_players:
        raise RoomConflictError(f"房间已满（{room.max_players} 人）")

    if nickname is None:
        taken = {p.nickname for p in existing}
        nickname = next(
            (n for n in _DEFAULT_NICKNAMES if n not in taken),
            f"AI-{len(existing) + 1}",
        )
    return await create_ai_player(
        db, room_id, nickname=nickname, occupation_name=occupation_name, seed=seed
    )


async def create_ai_player(
    db: AsyncSession,
    room_id: str,
    *,
    nickname: str,
    occupation_name: str | None = None,
    seed: int | None = None,
) -> Player:
    """在房间里加一个 AI 玩家，并给它一张**规则上合法**的完成态角色卡。

    这层不做鉴权与人数校验（那是 `add_ai_player_to_room` 的事）——测试与试玩
    装置直接用这个，不必先造一个房主凭证。

    `seed` 用于可复现（测试与试玩装置要能造出同一张卡）。不传就用系统随机。
    """
    room = await db.get(Room, room_id)
    if room is None:
        raise ValueError(f"房间不存在：{room_id}")

    sheet = roll_character_sheet(occupation_name=occupation_name, seed=seed)
    occupation, attributes, skills = sheet.occupation, sheet.attributes, sheet.skills

    # 🔴 `ready=True` 不是图省事：大厅的「全员就绪」按非房主玩家逐个判，而 AI
    # 没有连接、永远点不了那个按钮——留 False 会让房主的「开始游戏」永久点不亮。
    # 它一落座就带着一张完成态的卡，"就绪"对它是事实描述而不是待办。
    player = Player(room_id=room_id, nickname=nickname, is_ai=True, has_character=True, ready=True)
    db.add(player)
    await db.flush()
    db.add(
        Character(
            room_id=room_id,
            player_id=player.id,
            status="complete",
            name=nickname,
            occupation_id=occupation.id,
            occupation=occupation.name,
            age=_AI_AGE,
            gender="未知",
            attributes=attributes,
            # 30 岁没有年龄修正 → 分配值与有效值相同，两份存同一套
            allocated_attributes=dict(attributes),
            derived_stats=compute_derived_stats(attributes, _AI_AGE),
            skills=skills,
            generation_method=GENERATION_POINT_BUY,
        )
    )
    await db.commit()
    return player


async def count_ai_players(db: AsyncSession, room_id: str) -> int:
    rows = await db.execute(
        select(Player.id).where(Player.room_id == room_id, Player.is_ai.is_(True))
    )
    return len(list(rows.scalars()))
