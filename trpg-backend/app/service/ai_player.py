"""AI 玩家的座位与角色卡（exec/21 第二层）。

## 为什么不走建卡流程

建卡的 HTTP 三步（POST draft → PATCH → POST complete）是**前端向导的形状**，
不是规则的形状：规则校验本来就在 service 层（`complete_character` 调
`coc7/rules.py` 的 validate_character + compute_derived_stats）。为了造一行数据走
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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.background_writer import BackgroundWriter
from app.core.coc7.rules import GENERATION_POINT_BUY, compute_derived_stats
from app.models.room import Character, Player, Room
from app.service.character_background import generate_background
from app.service.character_generator import (
    _allocate_attributes,
    _allocate_skills,
    roll_character_sheet,
)

#: 🔴 生成器本身已经搬去 `service/character_generator.py`（2026-08-10）：它原本
#: 住在这里，而玩家的「一键生成」import 它，于是玩家继承了一整套按"AI 补位"定的
#: 默认值（兴趣点不花、固定 30 岁）。**一份东西扮演两个角色必出结构性 bug。**
#:
#: 这三个名字保留成再导出，是因为**现有测试从这里注入**
#: （`tests/test_ai_character.py` 直接 import 两个私有函数）——判据同项目
#: CLAUDE.md：逻辑搬走、**接缝留下**。
__all__ = ["_allocate_attributes", "_allocate_skills", "roll_character_sheet"]

#: AI 调查员固定 30 岁。COC7 的年龄修正在 20–39 岁区间内为零，于是「分配值」
#: 与「有效值」两份属性完全相同——不需要为 AI 玩家维护年龄修正那套双份记账
#: （见 character-build-migration round4 方案 A）。这是有意的简化，不是遗漏：
#: **AI 玩家的年龄不影响任何玩法**。
#:
#: 🔴 这条判断对真人玩家不成立，所以它现在是**调用方传给生成器的参数**，
#: 不再是生成器自己的默认值——否则每个用一键生成的新人都是 30 岁。
_AI_AGE = 30

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
    writer: BackgroundWriter | None = None,
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
        db,
        room_id,
        nickname=nickname,
        occupation_name=occupation_name,
        seed=seed,
        writer=writer,
    )


async def create_ai_player(
    db: AsyncSession,
    room_id: str,
    *,
    nickname: str,
    occupation_name: str | None = None,
    seed: int | None = None,
    writer: BackgroundWriter | None = None,
) -> Player:
    """在房间里加一个 AI 玩家，并给它一张**规则上合法**的完成态角色卡。

    这层不做鉴权与人数校验（那是 `add_ai_player_to_room` 的事）——测试与试玩
    装置直接用这个，不必先造一个房主凭证。

    `seed` 用于可复现（测试与试玩装置要能造出同一张卡）。不传就用系统随机。
    """
    room = await db.get(Room, room_id)
    if room is None:
        raise ValueError(f"房间不存在：{room_id}")

    sheet = roll_character_sheet(occupation_name=occupation_name, seed=seed, age=_AI_AGE)
    occupation, attributes, skills = sheet.occupation, sheet.attributes, sheet.skills

    # 🔴 `ready=True` 不是图省事：大厅的「全员就绪」按非房主玩家逐个判，而 AI
    # 没有连接、永远点不了那个按钮——留 False 会让房主的「开始游戏」永久点不亮。
    # 它一落座就带着一张完成态的卡，"就绪"对它是事实描述而不是待办。
    player = Player(room_id=room_id, nickname=nickname, is_ai=True, has_character=True, ready=True)
    db.add(player)
    await db.flush()
    # AI 队友跟真人的一键生成走同一条背景路径：它的有限视角里也只有职业和技能
    # （`ai_actor.build_view`），有段过去它才像个人而不是一具技能表。
    written = await generate_background(
        db,
        room_id,
        writer,
        name=nickname,
        occupation=occupation.name,
        age=_AI_AGE,
        skills=skills,
    )
    background, background_detail = written if written is not None else (None, None)
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
            background=background,
            background_detail=background_detail,
        )
    )
    await db.commit()
    return player


async def count_ai_players(db: AsyncSession, room_id: str) -> int:
    rows = await db.execute(
        select(Player.id).where(Player.room_id == room_id, Player.is_ai.is_(True))
    )
    return len(list(rows.scalars()))
