"""Service 层：角色（调查员）建卡（issue #59，issue #77 切换为真实 ORM 读写
+ 补齐服务端权威掷骰 / 角色卡模板两个新协议位置）。

建卡流程分两段：POST 创建草稿 → PATCH 保存完整数据 → POST complete 标记完成。
房间/重连凭证校验复用 service/room.py 的 `get_player_by_reconnect_token`——
角色卡操作跟房间操作共用同一套"这是房间里的哪个玩家"身份体系。
"""

import random
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.background_writer import BackgroundWriter
from app.core.coc7.age import (
    apply_app_loss,
    distribute_scd_loss,
    get_age_modifiers,
    roll_edu_improvement,
)
from app.core.coc7.content import build_coc7_ruleset
from app.core.coc7.rules import (
    GENERATION_POINT_BUY,
    GENERATION_ROLL,
    GENERATION_ROLL_POOL,
    ValidationIssue,
    compute_derived_stats,
    compute_preview,
    find_occupation_by_id,
    find_occupation_by_name,
    validate_age,
    validate_character_with_occupation,
)
from app.core.errors import not_implemented
from app.dto.character import (
    AgeAdjustmentResult,
    AttributePoolRollView,
    CharacterComputeResult,
    CharacterDraftResult,
    CharacterPreviewRequest,
    CharacterRead,
    CharacterTemplateCreateBody,
    CharacterTemplateRead,
    CharacterUpdateBody,
    EduImprovementCheckView,
    PartyCharacterRead,
    RollAttributePoolResult,
    RollAttributesResult,
    RollLuckResult,
)
from app.dto.game import RulesetRead
from app.models.room import Character, Player, Room
from app.service.character_background import generate_background
from app.service.room import (
    RoomAuthorizationError,
    find_room_by_id,
    get_player_by_reconnect_token,
    require_ruleset,
)


class CharacterNotFoundError(ValueError):
    """角色不存在。"""


class AttributesNotSetError(ValueError):
    """还没生成过属性（掷骰/点数购买/掷点池都没跑过）就调用需要属性的操作
    （比如 apply-age-adjustment）——没有可扣减的对象。"""


class AlreadyRolledError(ValueError):
    """已经掷过一次，不允许重掷（wizard-bugfix-round1 #4④）：`roll_attribute_pool`/
    `roll_luck` 各自只允许成功调用一次，前端隐藏重掷按钮防不住绕过 UI 直接
    重放请求，服务端才是权威。"""


class CharacterInvalidError(ValueError):
    """建卡数据未通过 COC7 权威校验（issue #84 S2）：`complete_character`
    落库前的最后一道闸门，不能只靠前端本地拦——S3 阶段前端本地规则计算会被
    删掉，这里是唯一权威来源。`issues` 是结构化校验报告，供 controller 层
    转成 `AppException.details` 带给前端。"""

    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        summary = "；".join(f"[{issue.code}] {issue.message}" for issue in issues)
        super().__init__(f"角色卡未通过校验：{summary}")


async def create_character_draft(
    db: AsyncSession, room_id: str, reconnect_token: str | None, based_on_template_id: str | None
) -> CharacterDraftResult:
    """房间内玩家创建一份角色草稿。

    `based_on_template_id`（issue #77 新增第三条建卡路径，issue 决策 5）：
    带了这个字段说明玩家想复用自己的常用卡，但"复制模板数据进草稿"这条读写
    本期没有实现（决策 5 原文：本期只铺表与接口，不实现），直接 NOT_IMPLEMENTED，
    不创建任何草稿；不带这个字段则完全是原来"从零建卡"的行为，不受影响。
    """
    if based_on_template_id is not None:
        raise not_implemented("复用常用角色卡本期尚未实现")

    room = await find_room_by_id(db, room_id)
    player = await get_player_by_reconnect_token(db, reconnect_token)
    if player.room_id != room.id:
        raise RoomAuthorizationError("你不在这个房间里")

    character = Character(room_id=room_id, player_id=player.id, status="draft")
    db.add(character)
    await db.commit()
    return CharacterDraftResult(character_id=character.id, status=character.status)


async def quick_build_character(
    db: AsyncSession,
    room_id: str,
    reconnect_token: str | None,
    name: str,
    writer: BackgroundWriter | None = None,
) -> CharacterDraftResult:
    """「一键生成」：给这个玩家造一张**规则上合法**的完成态角色卡。

    真人实测反馈（零基础玩家）：八步向导对新人不友好，光是"职业技能点该怎么
    分"就足以劝退。这条路径让他填个名字就能开局，卡仍然完全合法——生成器与
    AI 队友共用同一个（`ai_player.roll_character_sheet`），所以新手卡不会莫名
    其妙比 AI 队友弱或强。

    生成完就是 `complete`：这张卡不进向导。想改的人走原来那条路。

    🔴 角色名必填、不做兜底：名字是代入感的落点（建完卡之后守秘人就用它称呼
    你，见 `complete_character`），静默塞一个"无名调查员"只会让人以为坏了。
    """
    from app.service.ai_player import roll_character_sheet

    trimmed = (name or "").strip()
    if not trimmed:
        raise CharacterInvalidError(
            [ValidationIssue(code="NAME_REQUIRED", field="name", message="请先给调查员起个名字")]
        )

    room = await find_room_by_id(db, room_id)
    player = await get_player_by_reconnect_token(db, reconnect_token)
    if player.room_id != room.id:
        raise RoomAuthorizationError("你不在这个房间里")

    sheet = roll_character_sheet()
    existing = await db.scalar(
        select(Character).where(Character.room_id == room_id, Character.player_id == player.id)
    )
    character = (
        existing if existing is not None else Character(room_id=room_id, player_id=player.id)
    )
    character.status = "complete"
    character.name = trimmed
    character.occupation_id = sheet.occupation.id
    character.occupation = sheet.occupation.name
    character.age = sheet.age
    character.attributes = sheet.attributes
    # 生成器固定 30 岁 —— COC7 在这个区间没有年龄修正，分配值与有效值天然相同
    character.allocated_attributes = dict(sheet.attributes)
    character.derived_stats = compute_derived_stats(sheet.attributes, sheet.age)
    character.skills = sheet.skills
    character.generation_method = GENERATION_POINT_BUY
    # 背景是可选润色：写不出来（没配 key／超时／模型崩）就保持空，卡照常成立。
    written = await generate_background(
        db,
        room_id,
        writer,
        name=trimmed,
        occupation=sheet.occupation.name,
        age=sheet.age,
        skills=sheet.skills,
    )
    if written is not None:
        character.background, character.background_detail = written
    if existing is None:
        db.add(character)
    player.has_character = True
    # 跟 complete_character 同一条规矩：这一局里别人怎么叫你 = 角色名
    player.nickname = trimmed
    await db.commit()
    return CharacterDraftResult(character_id=character.id, status=character.status)


async def _get_own_character(
    db: AsyncSession, room_id: str, character_id: str, reconnect_token: str | None
) -> Character:
    player = await get_player_by_reconnect_token(db, reconnect_token)
    character = await db.get(Character, character_id)
    if character is None or character.room_id != room_id:
        raise CharacterNotFoundError("角色不存在")
    if character.player_id != player.id:
        raise RoomAuthorizationError("不能编辑其他玩家的角色")
    return character


async def update_character(
    db: AsyncSession,
    room_id: str,
    character_id: str,
    payload: CharacterUpdateBody,
    reconnect_token: str | None,
) -> None:
    """保存建卡向导算好的完整角色数据。"""
    character = await _get_own_character(db, room_id, character_id, reconnect_token)

    # PR #97 review [1]：`roll` 这个来源标记不能跨越「客户端自己重填了属性」。
    # `roll_attributes` 会把它置成 roll，complete 时据此跳过 480 点预算校验
    # （骰子结果本来就不受点数购买法约束）；但这个 PATCH 接受客户端任意属性，
    # 标记原样留着的话，先掷一次、再把 8 项全顶到 90 提交，就能绕开预算过关。
    # 属性跟掷出来那份不一致就说明是客户端填的，来源退回点数购买法。
    if (
        character.generation_method == GENERATION_ROLL
        and payload.attributes != character.attributes
    ):
        character.generation_method = GENERATION_POINT_BUY
    elif payload.generation_method == GENERATION_POINT_BUY:
        # character-build-migration 已知缺口：`roll_pool` 没有类似上面的
        # 自动回退——玩家掷完点池池、又点前端"点数购买"按钮改回手动分配，
        # 此前后端从不知道这次切换，complete 时仍按 roll_pool 的"总和必须
        # 精确等于池子总值"校验，会把一个正常的点数购买法分配错判成
        # ATTRIBUTE_POOL_MISMATCH。这里只信任客户端"降级到点数购买"这一个
        # 方向（见 CharacterUpdateBody.generation_method 的字段说明，点数
        # 购买本身校验最严，声明切到它不可能绕开任何约束），不接受借这个
        # 字段自称 roll/roll_pool。
        character.generation_method = GENERATION_POINT_BUY
        character.attribute_pool_total = None

    character.name = payload.name
    character.age = payload.age
    character.gender = payload.gender
    character.residence = payload.residence
    character.birthplace = payload.birthplace
    character.attributes = payload.attributes
    # 玩家分配的原始属性（年龄修正之前，wizard-bugfix-round4 方案 A）。直接
    # 赋值，`None` 也照写——客户端明确不带就是没有这份数据。
    character.allocated_attributes = payload.allocated_attributes
    character.derived_stats = payload.derived_stats
    character.skills = payload.skills
    character.equipment = [item.name for item in payload.equipment]
    # 职业用 id 定位（exec/22）。老客户端不传 id 时保持 None，complete 时
    # 回退按名字查——行为与改动前一致，不会把老前端弄挂。
    character.occupation_id = payload.occupation_id
    character.occupation = payload.occupation
    character.background = payload.background
    character.notes = payload.notes
    character.background_detail = payload.background_detail
    await db.commit()


async def _resolve_ruleset(db: AsyncSession, room: Room) -> RulesetRead:
    """决定这个房间的建卡该用哪份规则数据（issue #112：`coc7_rules` 本身对
    具体是哪个规则系统无知，取数据这件事由 service 层负责）。

    房间已经选了模组（`room.system_id` 有值）就用那个规则系统的 ruleset；
    房间还没选模组（比如还在大厅、玩家提前建卡）时没有 `system_id` 可查，
    这里回落到内置 COC7——这是当前唯一内置的规则系统，默认值刻意放在这层
    组装代码里，而不是塞进 `coc7_rules.py`：规则核心保持对具体系统无知，
    正是 issue #112 参数注入的整个目的。

    走 `require_ruleset` 而不是 `get_ruleset`：这是裁决路径，规则数据为空时
    必须拒绝而不是当成"零个约束"放行（见 `require_ruleset` 的说明）。
    """
    if room.system_id is not None:
        return await require_ruleset(db, room.system_id)
    return build_coc7_ruleset()


async def complete_character(
    db: AsyncSession, room_id: str, character_id: str, reconnect_token: str | None
) -> None:
    """标记建卡完成，同步把对应玩家的 has_character 置为 True。

    issue #84 S2：落库前先用 `coc7_rules` 权威校验已保存的属性/职业/技能是否
    合法，不合法直接抛 `CharacterInvalidError` 拒绝；映射不到职业时校验会产出
    `OCCUPATION_NOT_FOUND`，同样被拒绝，不会静默放行。

    🔴 **职业优先按 id 定位**（exec/22）：职业名不唯一——规则表里有 6 组同名
    不同项的职业（律师 ×2、私家侦探 ×2、工匠 ×2…），信用区间乃至技能点公式
    都不同。只按名字查会拿回第一个匹配，于是合法的卡可能被判非法；公式不同
    的那三组更阴，会把职业技能点预算算成另一个数**且不报错**。
    `occupation_id` 为空（老卡 / 老客户端）时回退按名字查，行为与改动前一致。
    """
    character = await _get_own_character(db, room_id, character_id, reconnect_token)
    room = await find_room_by_id(db, room_id)
    ruleset = await _resolve_ruleset(db, room)

    if character.occupation_id is not None:
        occupation, not_found = find_occupation_by_id(ruleset.occupations, character.occupation_id)
    else:
        occupation, not_found = find_occupation_by_name(ruleset.occupations, character.occupation)

    issues = validate_age(ruleset, character.age) + validate_character_with_occupation(
        ruleset,
        attributes=character.attributes or {},
        occupation=occupation,
        skills=character.skills or {},
        generation_method=character.generation_method,
        attribute_pool_total=character.attribute_pool_total,
        # wizard-bugfix-round4（方案 A，#20 的修复）：年龄修正后的有效值不能
        # 再拿去跑只对分配值成立的预算/池值总和/步进校验，见
        # `validate_character` 的 `allocated_attributes` 参数说明。没有分配值
        # 的老角色卡传 None，退回旧行为。
        allocated_attributes=character.allocated_attributes,
        occupation_not_found=not_found,
    )
    if issues:
        raise CharacterInvalidError(issues)

    # PR #85 review #3：校验通过后属性一定合法，衍生值改成服务端权威重算
    # 并覆盖——不再信任客户端 PATCH 上来的 `derived_stats`，避免属性合法但
    # HP/SAN 被客户端乱填过关。`character.age` 一并传入：MOV 要扣年龄惩罚
    # （见 coc7_rules.compute_derived_stats），角色卡本来就存了 age，这里
    # 是唯一真正落定衍生值的地方，不传的话年龄调整端点算出的 MOV 惩罚永远
    # 不会体现在角色卡上。
    character.derived_stats = compute_derived_stats(character.attributes or {}, character.age)
    character.status = "complete"
    player = await db.get(Player, character.player_id)
    if player is not None:
        player.has_character = True
        # 🔴 建完卡之后，这个房间里的显示名 = **角色名**（真人实测反馈）。
        # 玩家给调查员起了名字，守秘人却还在叫他的账号昵称——桌上没人会用
        # "登录名"称呼你的角色，代入感当场破掉。
        #
        # 改在这里而不是各个展示点：`Player.nickname` 是全链路唯一的称呼来源
        # （原话广播、守秘人 roster 与历史行、检定请求、队友列表都读它），
        # 逐处去 join 角色卡既啰嗦又必然漏掉一处。账号昵称仍留在 `users` 表上，
        # 这里改的只是"在这一局里别人怎么叫你"。
        name = (character.name or "").strip()
        if name:
            player.nickname = name
    await db.commit()


async def get_character(
    db: AsyncSession, room_id: str, character_id: str, reconnect_token: str | None
) -> CharacterRead:
    """GET /rooms/{roomId}/characters/{characterId} —— 读回自己的角色卡
    （issue #96）。

    鉴权复用 `_get_own_character`：只能读自己那张，不能拿别人的角色卡——
    角色卡里有背景故事、装备这些属于该玩家的信息，房间内其他人不该直接拉到。
    """
    character = await _get_own_character(db, room_id, character_id, reconnect_token)
    return CharacterRead(
        id=character.id,
        status=character.status,
        generation_method=character.generation_method,
        attribute_pool_total=character.attribute_pool_total,
        name=character.name,
        age=character.age,
        gender=character.gender,
        residence=character.residence or "",
        birthplace=character.birthplace or "",
        attributes=character.attributes or {},
        allocated_attributes=character.allocated_attributes,
        derived_stats=character.derived_stats or {},
        skills=character.skills or {},
        equipment=list(character.equipment or []),
        occupation_id=character.occupation_id,
        occupation=character.occupation,
        background=character.background or "",
        notes=character.notes or "",
        background_detail=character.background_detail,
    )


class BackgroundUnavailableError(RuntimeError):
    """背景没能生成出来（没配 key／超时／模型崩）。

    🔴 跟 `quick_build_character` 里的处理**故意相反**：那里写不出来就保持空、
    卡照常成立（玩家在等的是卡，背景是锦上添花）；这里玩家**主动点了「换一个」**，
    静默保持原样会让他以为按钮坏了、然后一直点。显式失败，告诉他。
    """


async def regenerate_background(
    db: AsyncSession,
    room_id: str,
    character_id: str,
    reconnect_token: str | None,
    writer: BackgroundWriter | None = None,
) -> CharacterRead:
    """重摇一次角色背景（exec/25 P1 #5）。

    `exec/20 §1.9` 里定的方向：不打算硬化"写得好不好"——真要管内容质量，
    该给玩家一个重新生成的按钮，让人来判，而不是让代码去判。

    只重写背景，**属性/技能/职业一个都不动**：玩家想换的是这个人的过去，
    不是换一张卡（换卡他可以回去重新一键生成）。
    """
    character = await _get_own_character(db, room_id, character_id, reconnect_token)
    written = await generate_background(
        db,
        room_id,
        writer,
        name=character.name or "",
        occupation=character.occupation or "",
        age=character.age or 0,
        skills=character.skills or {},
    )
    if written is None:
        raise BackgroundUnavailableError("背景生成失败")
    character.background, character.background_detail = written
    await db.commit()
    return await get_character(db, room_id, character_id, reconnect_token)


async def list_party_characters(
    db: AsyncSession, room_id: str, reconnect_token: str | None
) -> list[PartyCharacterRead]:
    """GET /rooms/{roomId}/characters —— 看队友的角色卡（exec/14 P5.3）。

    鉴权只要求**你是这个房间里的人**（凭证对应的玩家属于该房间），不像
    `_get_own_character` 那样要求"是你自己那张"。这是有意的：真人桌上角色卡
    互相传阅，此前系统只能读回自己那张，比真人桌还封闭（exec/18 ⑨）。

    返回房间内**全部**玩家（含自己、含还没建卡的），按加入顺序。没建卡的人
    也要出现——"谁还没准备好"本身就是队伍面板要回答的问题；此时除
    `player_id`/`nickname`/`status` 外都是空值，`id` 为空串。
    """
    player = await get_player_by_reconnect_token(db, reconnect_token)
    if player.room_id != room_id:
        raise RoomAuthorizationError("你不在这个房间里")

    roster_query = (
        select(Player).where(Player.room_id == room_id).order_by(Player.joined_at, Player.id)
    )
    players = list((await db.execute(roster_query)).scalars())
    characters = list(
        (await db.execute(select(Character).where(Character.room_id == room_id))).scalars()
    )
    by_player = {c.player_id: c for c in characters}

    out: list[PartyCharacterRead] = []
    for p in players:
        # AI 玩家也是队友，它的卡照样要能被传阅（exec/21 第一层）。
        c = by_player.get(p.id)
        if c is None:
            out.append(
                PartyCharacterRead(player_id=p.id, nickname=p.nickname, id="", status="absent")
            )
            continue
        out.append(
            PartyCharacterRead(
                player_id=p.id,
                nickname=p.nickname,
                id=c.id,
                status=c.status,
                name=c.name,
                age=c.age,
                gender=c.gender,
                residence=c.residence or "",
                birthplace=c.birthplace or "",
                attributes=c.attributes or {},
                derived_stats=c.derived_stats or {},
                skills=c.skills or {},
                equipment=list(c.equipment or []),
                occupation_id=c.occupation_id,
                occupation=c.occupation,
                background=c.background or "",
                background_detail=c.background_detail,
            )
        )
    return out


def compute_character_preview(
    ruleset: RulesetRead, payload: CharacterPreviewRequest
) -> CharacterComputeResult:
    """POST /api/v1/systems/{systemId}/character/preview —— 建卡过程中的权威
    计算预览（issue #84 S2，路线乙的接缝）：不碰数据库，纯函数式地把
    `coc7_rules.compute_preview` 的结果转成 DTO。

    `ruleset` 由调用方（controller）取好传入（issue #112）——它已经为了校验
    systemId 存在而查过一次 `get_ruleset`，这里直接复用结果，不重新查一次。
    """
    result = compute_preview(
        ruleset,
        attributes=payload.attributes,
        occupation_id=payload.occupation_id,
        skills=payload.skills,
        age=payload.age,
        generation_method=payload.generation_method or GENERATION_POINT_BUY,
        attribute_pool_total=payload.attribute_pool_total,
        allocated_attributes=payload.allocated_attributes,
    )
    return CharacterComputeResult(**asdict(result))


def _roll(n: int, sides: int) -> int:
    return sum(random.randint(1, sides) for _ in range(n))


async def roll_attributes(
    db: AsyncSession, room_id: str, character_id: str, reconnect_token: str | None
) -> RollAttributesResult:
    """POST /rooms/{roomId}/characters/{characterId}/roll-attributes —— 服务端
    权威掷骰生成属性（issue #77 新增，取代前端 `Math.random()` 本地算骰值）。

    COC7 标准生成法：STR/CON/DEX/APP/POW/LUCK = 3d6*5，SIZ/INT/EDU = (2d6+6)*5；
    幸运（LUCK）独立掷骰、不由属性推导，也不参与技能点公式，但它是游戏中可被
    消耗的有状态属性，所以跟其余 8 项一起放在 `attributes` 里而非衍生值；
    衍生值按标准公式：HP = (CON+SIZ)/10 取整，MP = POW/5 取整，SAN = POW*5
    （起始理智等于 POW 的 5 倍，跟 POW 属性值本身相等，这里遵循 COC7 规则
    直接抄一份 POW*5 = 属性打点后的数值）。

    注意这跟"三处原型取舍"表格里的 `check.*`（游戏中的技能/理智检定）是两回事：
    这里是建卡阶段生成初始属性的纯随机数生成，不涉及规则引擎裁决，本期就是
    真实实现，不是 NOT_IMPLEMENTED 桩。
    """
    character = await _get_own_character(db, room_id, character_id, reconnect_token)

    attributes = {
        "STR": _roll(3, 6) * 5,
        "CON": _roll(3, 6) * 5,
        "DEX": _roll(3, 6) * 5,
        "APP": _roll(3, 6) * 5,
        "POW": _roll(3, 6) * 5,
        "SIZ": (_roll(2, 6) + 6) * 5,
        "INT": (_roll(2, 6) + 6) * 5,
        "EDU": (_roll(2, 6) + 6) * 5,
        "LUCK": _roll(3, 6) * 5,
    }
    derived_stats = {
        "HP": (attributes["CON"] + attributes["SIZ"]) // 10,
        "MP": attributes["POW"] // 5,
        "SAN": attributes["POW"],
    }

    character.attributes = attributes
    character.derived_stats = derived_stats
    # 标记这张卡的属性是掷出来的：complete 时不能拿点数购买法的总预算去卡它
    # （8 项总和均值约 457、范围 195–720，经常超 480）。见 issue #96 决策 1。
    character.generation_method = GENERATION_ROLL
    await db.commit()
    return RollAttributesResult(attributes=attributes, derived_stats=derived_stats)


def _roll_dice(n: int, sides: int) -> list[int]:
    """跟 `_roll` 一样服务端权威掷骰，但保留每个骰子的原始点数——掷点池法
    要把明细（不只是求和结果）返回给玩家核对。"""
    return [random.randint(1, sides) for _ in range(n)]


async def roll_attribute_pool(
    db: AsyncSession, room_id: str, character_id: str, reconnect_token: str | None
) -> RollAttributePoolResult:
    """POST /rooms/{roomId}/characters/{characterId}/roll-attribute-pool ——
    掷点池生成法（迁移自 coc-char-gen `js/core/dice.js::rollAttributePointPool`）：
    5 次 3d6×5 + 3 次 (2d6+6)×5 求和成一个总点数池，玩家再手动分配到八维。

    跟 `roll_attributes` 的区别：这里**不写** `character.attributes`——分配
    是后续 PATCH 完成的，这里只记下服务端权威的总值（`attribute_pool_total`），
    complete 时据此校验"玩家分配的总和是否等于池子总值"。
    """
    character = await _get_own_character(db, room_id, character_id, reconnect_token)
    if character.attribute_pool_total is not None:
        raise AlreadyRolledError("属性点数池已经掷过一次，不允许重掷。")

    rolls: list[AttributePoolRollView] = []
    total = 0
    for _ in range(5):
        dice = _roll_dice(3, 6)
        value = sum(dice) * 5
        rolls.append(AttributePoolRollView(kind="3d6x5", dice=dice, value=value))
        total += value
    for _ in range(3):
        dice = _roll_dice(2, 6)
        value = (sum(dice) + 6) * 5
        rolls.append(AttributePoolRollView(kind="2d6+6x5", dice=dice, value=value))
        total += value

    character.generation_method = GENERATION_ROLL_POOL
    character.attribute_pool_total = total
    await db.commit()
    return RollAttributePoolResult(rolls=rolls, total=total)


async def roll_luck(
    db: AsyncSession, room_id: str, character_id: str, reconnect_token: str | None
) -> RollLuckResult:
    """POST /rooms/{roomId}/characters/{characterId}/roll-luck —— 幸运单掷
    （character-build-migration redesign-v2 §4-A）：3d6×5，独立于属性生成
    方式——点数购买法的玩家同样需要掷幸运（`AttributeSpec.pointBuy=false`
    已经说明它不参与购买），塞进 `roll_attribute_pool` 的话点数购买法玩家
    就掷不到，所以单开一个端点服务三种生成方式。

    只改 `character.attributes["LUCK"]` 这一个键，不动其它属性、不动
    `generation_method`——跟 `roll_attributes`/`roll_attribute_pool` 不同，
    这里不改变这张卡的属性生成方式。

    15–19 岁的"幸运掷两次取高"由 `apply_age_adjustment` 负责
    （`luckRerolled` 字段），这里不判年龄，避免同一条规则两处实现。
    """
    character = await _get_own_character(db, room_id, character_id, reconnect_token)
    if "LUCK" in (character.attributes or {}):
        raise AlreadyRolledError("幸运已经掷过一次，不允许重掷。")

    dice = _roll_dice(3, 6)
    value = sum(dice) * 5

    attributes = dict(character.attributes or {})
    attributes["LUCK"] = value
    character.attributes = attributes
    await db.commit()
    return RollLuckResult(kind="3d6x5", dice=dice, value=value)


async def apply_age_adjustment(
    db: AsyncSession,
    room_id: str,
    character_id: str,
    age: int,
    reconnect_token: str | None,
) -> AgeAdjustmentResult:
    """POST /rooms/{roomId}/characters/{characterId}/apply-age-adjustment ——
    按 COC7 年龄表（`app/core/coc7_age.py`，迁移自 coc-char-gen
    `js/plugins/age.js`）套用建卡期年龄修正：EDU 改进检定 / STR-SIZ 或
    STR-CON-DEX 减值 / APP 减值 / 青年幸运双掷。

    必须先有属性（掷骰/点数购买/掷点池三条路径之一产出的八维）才能套用——
    没有属性就没有可扣减的对象，直接拒绝而不是套用到一份空字典上。

    wizard-bugfix-round4（方案 A，#18 的修复）：年龄修正永远基于「分配值」
    （`character.allocated_attributes`）重算，而不是在上一次修正的结果上
    再修一次——后者会把扣减累加（实测 45 岁套两次：STR+CON+DEX 共扣 10，
    规则规定 5）。这样一来同一张卡重复调用（比如玩家改了年龄又调一次）就是
    幂等的：每次都从干净的分配值出发。没有分配值的老角色卡回落到
    `character.attributes`，保持原行为（不幂等，前端只调用一次即可规避）。
    """
    character = await _get_own_character(db, room_id, character_id, reconnect_token)
    source_attributes = (
        character.allocated_attributes
        if character.allocated_attributes is not None
        else character.attributes
    )
    if not source_attributes:
        raise AttributesNotSetError("必须先生成属性才能应用年龄调整")

    attributes_before = dict(source_attributes)
    attributes = dict(attributes_before)
    modifiers = get_age_modifiers(age)

    edu_checks: list[EduImprovementCheckView] = []
    for _ in range(modifiers.edu_checks):
        edu_before = attributes.get("EDU", 0)
        success, roll, gain, edu_after = roll_edu_improvement(edu_before)
        edu_checks.append(
            EduImprovementCheckView(
                success=success, roll=roll, gain=gain, edu_before=edu_before, edu_after=edu_after
            )
        )
        attributes["EDU"] = edu_after

    if modifiers.edu_flat:
        attributes["EDU"] = max(1, attributes.get("EDU", 0) + modifiers.edu_flat)

    if modifiers.luck_twice and "LUCK" in attributes:
        attributes["LUCK"] = max(_roll(3, 6) * 5, _roll(3, 6) * 5)

    if modifiers.scd_loss:
        attributes = distribute_scd_loss(attributes, modifiers.scd_loss, modifiers.str_siz_only)

    if modifiers.app_loss:
        attributes = apply_app_loss(attributes, modifiers.app_loss)

    character.attributes = attributes
    character.age = age
    await db.commit()

    scd_affected: list[str] = []
    if modifiers.scd_loss:
        scd_affected = ["STR", "SIZ"] if modifiers.str_siz_only else ["STR", "CON", "DEX"]
    return AgeAdjustmentResult(
        age=age,
        age_label=modifiers.label,
        attributes_before=attributes_before,
        attributes_after=attributes,
        edu_checks=edu_checks,
        edu_flat_adjustment=modifiers.edu_flat,
        scd_loss=modifiers.scd_loss,
        scd_affected_attributes=scd_affected,
        app_loss=modifiers.app_loss,
        luck_rerolled=modifiers.luck_twice,
        mov_penalty=modifiers.mov_penalty,
    )


# ── 我的常用角色卡库（issue 决策 5：本期只铺表与接口，不实现真实读写） ──


async def list_character_templates(db: AsyncSession, user_id: str) -> list[CharacterTemplateRead]:
    raise not_implemented("我的常用角色卡库本期尚未实现")


async def create_character_template(
    db: AsyncSession, user_id: str, payload: CharacterTemplateCreateBody
) -> CharacterTemplateRead:
    raise not_implemented("我的常用角色卡库本期尚未实现")


async def get_character_template(
    db: AsyncSession, user_id: str, template_id: str
) -> CharacterTemplateRead:
    raise not_implemented("我的常用角色卡库本期尚未实现")


async def delete_character_template(db: AsyncSession, user_id: str, template_id: str) -> None:
    raise not_implemented("我的常用角色卡库本期尚未实现")
