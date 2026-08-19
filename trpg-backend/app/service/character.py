"""Service 层：角色（调查员）建卡（issue #59，issue #77 切换为真实 ORM 读写
+ 补齐服务端权威掷骰 / 角色卡模板两个新协议位置）。

建卡流程分两段：POST 创建草稿 → PATCH 保存完整数据 → POST complete 标记完成。
房间/重连凭证校验复用 service/room.py 的 `get_player_by_reconnect_token`——
角色卡操作跟房间操作共用同一套"这是房间里的哪个玩家"身份体系。
"""

import contextlib
import random
from dataclasses import asdict

import structlog
from sqlalchemy import func, select, update
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
    resolve_max_hp,
    validate_age,
    validate_character_with_occupation,
)
from app.core.config import get_settings
from app.core.equipment_check import (
    EquipmentChecker,
    clamp_items,
    rejection_message,
)
from app.core.equipment_check import build_prompt as build_equipment_prompt
from app.dto.character import (
    AgeAdjustmentResult,
    AttributePoolRollView,
    CharacterComputeResult,
    CharacterDraftResult,
    CharacterPreviewRequest,
    CharacterRead,
    CharacterTemplateCreateBody,
    CharacterTemplateOverwriteBody,
    CharacterTemplateRead,
    CharacterTemplateUpdateBody,
    CharacterUpdateBody,
    EduImprovementCheckView,
    EquipmentCheckBody,
    EquipmentCheckResult,
    PartyCharacterRead,
    RejectedEquipmentView,
    RollAttributePoolResult,
    RollAttributesResult,
    RollLuckResult,
)
from app.dto.game import RulesetRead
from app.models.room import Character, Player, Room
from app.models.user import UserCharacterTemplate
from app.service.character_background import generate_background, module_era
from app.service.room import (
    RoomAuthorizationError,
    find_room_by_id,
    get_player_by_reconnect_token,
    require_ruleset,
)

logger = structlog.get_logger()


class CharacterTemplateNotFoundError(ValueError):
    """常用卡不存在、或不属于这个账号、或规则系统对不上。

    三种情况共用一个错误：对调用方来说它们是同一件事（这张卡你用不了），
    而分开报会把"这个 id 确实存在，只是不是你的"泄露出去。
    """


class CharacterTemplateNotEditableError(ValueError):
    """想在卡库里改的字段不在可改白名单里（属性/年龄/职业/技能这些规则数）。

    显式拒绝而不是静默丢弃：静默丢弃的话，前端以为改上去了、界面也显示改了，
    刷新一下又变回原样——这种 bug 前后端两头都不会变红。
    """


class CharacterTemplateLimitReachedError(ValueError):
    """卡库已满（`TEMPLATE_LIMIT`）。

    单独一个错误类而不是复用 `CharacterTemplateNotFoundError`：这条要让玩家
    看懂"删几张就能继续"，混进"这张卡你用不了"里他只会以为坏了。
    """


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

    `based_on_template_id`（第三条建卡路径）：复用玩家自己的常用卡，把模板里
    的建卡态整份复制进新草稿。**复制，不是引用**——这一局怎么玩坏都不会回头
    改动卡库里那张。不带这个字段则完全是原来"从零建卡"的行为。

    🔴 **合法就直接建成 `complete`，不把人再赶进向导一遍**（2026-08-13 真人
    反馈：「我选择自己常用的角色卡之后，为什么还要我进行下一步呀？」）。
    卡库里那张是**已经建完的**卡——让玩家把八步再走一遍，唯一的产出是把他
    刚选的东西原样确认一次。

    校验一条都不少，只是**换了执行的人**：这里直接调 `complete_character`，
    走的是同一道闸门（模板存的时候合法，不代表在**这个**房间的规则系统下
    还合法）。**校验没过才退回 `draft`**，让玩家进向导修——那时候向导是
    "修一张具体哪里不合法的卡"，不是"再确认一遍"。
    """
    room = await find_room_by_id(db, room_id)
    player = await get_player_by_reconnect_token(db, reconnect_token)
    if player.room_id != room.id:
        raise RoomAuthorizationError("你不在这个房间里")

    # 🔴 **一个玩家在一个房间只有一张卡**：复用已有那行，不新建。
    #
    # 此前这里每次都新建，而 `quick_build_character` 复用——同一件事两种做法，
    # 于是连点几次「用我的常用卡」就留下几张。危害不在脏数据本身，而在**哪张
    # 算数两边不一致**：重连走 `db.scalar(...)` 取第一行（`service/room.py`），
    # 队伍面板与守秘人走 `{player_id: c}` 覆盖成最后一行（两处都没 ORDER BY，
    # 连"最后一张"都不是保证）。症状是玩家在准备页编辑的卡跟桌上生效的不是
    # 同一张。
    character = await db.scalar(
        select(Character).where(Character.room_id == room_id, Character.player_id == player.id)
    )
    if character is None:
        character = Character(room_id=room_id, player_id=player.id, status="draft")
    elif based_on_template_id is not None:
        # 换成卡库那张：旧的要**整张让位**，不能只盖掉模板带来的字段（残留的
        # 技能/装备会跟新卡混在一起）。
        #
        # 🔴 只在"明确要换一张卡"时清。不带模板的调用**必须原样保留已有那张**：
        # 向导的 `ensureCharacterId` 在本地没有 characterId 时就会走这条路
        # （清掉浏览器存储、换台设备都会），清了等于把玩家已经建完的卡抹掉——
        # 那比本来要修的 bug 更糟。
        for field in _TEMPLATE_FIELDS:
            setattr(character, field, None)
        character.generation_method = GENERATION_POINT_BUY
        character.background = ""
        character.notes = ""
        character.status = "draft"
        character.based_on_template_id = None
    if based_on_template_id is not None:
        # 🔴 `Player.user_id` 可空（匿名入房）。卡库是账号级的，没有账号就没有
        # 卡库——要**显式说清楚**，不能让它掉进"常用卡不存在"里含糊过去。
        if player.user_id is None:
            raise CharacterTemplateNotFoundError("常用卡属于账号，登录之后才能复用")
        template = await _require_template(db, player.user_id, based_on_template_id)
        # 🔴 规则系统必须对得上：COC7 的卡不能拿去玩 DND5e（`system_id` 这一列
        # 存在的理由）。房间还没选模组时 system_id 是 None，那时也放行不了。
        if room.system_id != template.system_id:
            raise CharacterTemplateNotFoundError("这张常用卡不适用于本房间的规则系统")
        for field, value in (template.data or {}).items():
            if field in _TEMPLATE_FIELDS:
                setattr(character, field, value)
        character.based_on_template_id = template.id
    db.add(character)
    await db.commit()

    if based_on_template_id is not None:
        # 这张常用卡在本房间的规则下不合法时（换了规则系统、规则表改过…）**不抛
        # 给玩家**：草稿已经建出来了，抛异常等于让他连修的入口都没有。留在 draft，
        # 进向导改，那里会把具体是哪一条显示出来。
        #
        # 🔴 **不 rollback**：`complete_character` 先校验后落值，抛出来时一个字段
        # 都还没改，没有东西要回滚；而 rollback 会让 `character` 过期，紧接着读
        # `.status` 就变成一次同步 IO（MissingGreenlet）。
        with contextlib.suppress(CharacterInvalidError):
            await complete_character(db, room_id, character.id, reconnect_token)
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
    分"就足以劝退。这条路径让他填个名字就能开局，**卡跟老老实实走完向导的那张
    等价**：职业点与兴趣点都花完、年龄不是固定值。

    🔴 生成器在 `service/character_generator.py`，**不是** `ai_player`（2026-08-10
    搬的）：此前它住在 AI 那边、这里 import 过去，于是玩家继承了一整套按
    "AI 补位"定的默认值——兴趣点故意不花（理由写的是"AI 不该比真人玩家更强"，
    可玩家的卡就是它生成的，这条理由自相矛盾）、固定 30 岁。
    用户原话：**「一键生成走的应该是和真人自己加点一样的完整逻辑。」**

    生成完就是 `complete`：这张卡不进向导。想改的人走原来那条路。

    🔴 角色名必填、不做兜底：名字是代入感的落点（建完卡之后守秘人就用它称呼
    你，见 `complete_character`），静默塞一个"无名调查员"只会让人以为坏了。
    """
    from app.service.character_generator import roll_character_sheet

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
    # 生成器只在 20–39 岁里取 —— COC7 在这个区间没有年龄修正，分配值与有效值
    # 天然相同，不必在这条路径上维护那套双份记账（`DEFAULT_AGE_RANGE` 的说明）
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


async def _equipment_issues(
    db: AsyncSession,
    room: Room,
    character: Character,
    equipment_notes: dict[str, str] | None = None,
) -> list[ValidationIssue]:
    """装备合理性：这几件东西，这个人在这个时代这个地方拿得到吗？

    判据与 prompt 都在 `core/equipment_check.py`，这一层只做"取素材 + 调一次 +
    翻译成 `ValidationIssue`"。

    🔴 **没配 key / 调用失败 = 放行**，不是拦截。硬拦的对象是"判断结果为不合理"，
    不是"判断没跑成"——把可用性押给第三方服务不叫严格。CI 与 e2e 不配 key，
    走的正是这条路径。
    """
    items = clamp_items([item for item in (character.equipment or []) if isinstance(item, str)])
    if not items:
        return []
    api_key = get_settings().deepseek_api_key
    if not api_key:
        return []

    era = await module_era(db, room.scenario_id)
    verdict = await EquipmentChecker(api_key).check(
        build_equipment_prompt(
            equipment=items,
            occupation=character.occupation,
            age=character.age,
            residence=character.residence,
            birthplace=character.birthplace,
            credit_rating=(character.skills or {}).get("credit-rating"),
            era=era,
            notes=equipment_notes,
        )
    )
    if verdict is None:
        return []
    # 🔴 `field` 带上**是哪一件**：前端要就地给这件东西一个「说明来路」的输入框，
    # 只有一句拼好的话没法定位到具体哪一项。`field` 的既有语义就是字段路径
    # （`skills.spot-hidden`），沿用它，不新开 DTO 字段。
    return [
        ValidationIssue(
            code="EQUIPMENT_IMPLAUSIBLE",
            field=f"equipment.{rejected.item}",
            message=rejection_message(rejected),
        )
        for rejected in verdict.rejected
    ]


async def check_equipment(
    db: AsyncSession, room_id: str, reconnect_token: str | None, body: EquipmentCheckBody
) -> EquipmentCheckResult:
    """离开装备那一步时先审一遍，别让玩家一路填到最后才被拦回来。

    真人反馈（2026-08-19）：「在这里进行提示感觉很生硬呀，应该在装备那个界面
    点击下一步就该有」。原来唯一的闸门在 `complete_character`，那是**第 8 步**，
    而装备栏在**第 7 步**——中间隔着一整屏背景故事。

    🔴 **这不是第二道闸门，是同一道门的提前预览**：判据、prompt、放行规则全部
    复用 `_equipment_issues` 那条路，`complete` 那道仍然照跑。两份判据迟早
    分叉，而分叉的方向一定是"预览说行、提交说不行"。

    素材由调用方带上（向导那时还没 PATCH 过），只有 `era` 从模组取——它是
    服务端权威，让客户端传等于把"这一局是哪个年代"交给客户端说了算。
    """
    room = await find_room_by_id(db, room_id)
    player = await get_player_by_reconnect_token(db, reconnect_token)
    if player.room_id != room.id:
        raise RoomAuthorizationError("你不在这个房间里")

    items = clamp_items(body.equipment)
    if not items:
        return EquipmentCheckResult(checked=True, rejected=[])
    api_key = get_settings().deepseek_api_key
    if not api_key:
        return EquipmentCheckResult(checked=False, rejected=[])

    era = await module_era(db, room.scenario_id)
    verdict = await EquipmentChecker(api_key).check(
        build_equipment_prompt(
            equipment=items,
            occupation=body.occupation,
            age=body.age,
            residence=body.residence,
            birthplace=body.birthplace,
            credit_rating=body.credit_rating,
            era=era,
            notes=body.notes,
        )
    )
    if verdict is None:
        return EquipmentCheckResult(checked=False, rejected=[])
    return EquipmentCheckResult(
        checked=True,
        rejected=[
            RejectedEquipmentView(item=r.item, message=rejection_message(r))
            for r in verdict.rejected
        ],
    )


async def complete_character(
    db: AsyncSession,
    room_id: str,
    character_id: str,
    reconnect_token: str | None,
    equipment_notes: dict[str, str] | None = None,
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
    issues += await _equipment_issues(db, room, character, equipment_notes)
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
        hp_max=resolve_max_hp(character.derived_stats or {}, character.attributes or {}),
        skills=character.skills or {},
        equipment=list(character.equipment or []),
        occupation_id=character.occupation_id,
        occupation=character.occupation,
        background=character.background or "",
        notes=character.notes or "",
        background_detail=character.background_detail,
        based_on_template_id=character.based_on_template_id,
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
    按 COC7 年龄表（`app/core/coc7/age.py`，迁移自 coc-char-gen
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


# ── 我的常用角色卡库 ────────────────────────────────────────────────
#
# 2026-08-13 真正实现。此前是 issue 决策 5 留下的空壳：表、DTO、四个端点、
# SDK 方法全都铺好了，**service 层四个函数一律 `raise not_implemented`**，
# 前端一次都没调过。整条链每一层都在，就是没有一个人能用到它。
#
# 场景是线下的老玩家：这一晚开第二局、或者换个模组重开时，他不想再走一遍
# 八步向导。模板是**复制一份新的**，不是同一个调查员带着成长回来——后者要
# 战役支持，是另一件事，别混。

#: 复制进模板的建卡态字段。🔴 **显式命名成"逐个列出的地方"**：Character 加了
#: 新的建卡态列就要回来加一行，漏了不会有任何东西变红（新字段只是静默不进模板）。
#: 不进模板的是：id / room_id / player_id / status / based_on_* / 时间戳——
#: 那些要么是这一局的身份，要么是这张卡怎么来的。
_TEMPLATE_FIELDS = (
    "name",
    "age",
    "gender",
    "residence",
    "birthplace",
    "generation_method",
    "attribute_pool_total",
    "occupation_id",
    "occupation",
    "attributes",
    "allocated_attributes",
    "derived_stats",
    "skills",
    "equipment",
    "background",
    "notes",
    "background_detail",
)


def _restore_max_stats(derived: dict | None) -> dict | None:
    """把 `derived_stats` 归位到建卡时的上限。

    🔴 **模板不许带一身伤进新局。** keeper 改衍生值时把原值备份成 `{key}_MAX`、
    当前值留在 `key` 本身（见 `deps.write_stat`），所以一张玩过的卡的
    `derived_stats` 长这样：`{"HP": 3, "HP_MAX": 12}`。直接复制过去，新局
    开局就是残血——而模板按设计**只存建卡态，不带任何单局才有的状态**
    （`models/user.py` 那段注释）。

    有 `_MAX` 备份的用备份值还原、并丢掉 `_MAX` 键；没被改过的卡原样返回。
    """
    if not derived:
        return derived
    out = {}
    for key, value in derived.items():
        if key.endswith("_MAX"):
            continue
        out[key] = derived.get(f"{key}_MAX", value)
    return out


def character_to_template_data(character: Character) -> dict:
    """从一张角色卡提取建卡态。

    🔴 **提取规则在后端**，不让前端拼 `data` 传上来——"什么算建卡态"是规则
    知识（规则权威在后端），而且前端拼的话，Character 加一列就得两边同时改。
    """
    data = {field: getattr(character, field) for field in _TEMPLATE_FIELDS}
    data["derived_stats"] = _restore_max_stats(data["derived_stats"])
    return data


#: 一个账号最多存多少张常用卡。
#:
#: 上限存在的理由是**挡住脚本和误操作刷出上万张**，不是管理玩家——自己和朋友
#: 玩的量级离 50 很远。挡在这里而不是前端：前端拦不住直接打接口的调用方，而
#: "卡库能不能再存一张"是规则不是展示。
TEMPLATE_LIMIT = 50


async def list_character_templates(
    db: AsyncSession, user_id: str, system_id: str | None = None
) -> list[CharacterTemplateRead]:
    """我的卡库，最近更新的在前。

    `system_id` 给了就只返回**这个规则系统下能用的**那些。

    🔴 过滤在后端做，不让前端自己筛：`create_character_draft` 里那条
    「这张常用卡不适用于本房间的规则系统」才是权威判据，前端再实现一遍就是
    同一条规则落两处——两边哪天漂了，症状是浮层里明明列着的卡一点就报错。
    真机撞到的正是这个：挑卡浮层把 COC7 之外的卡也列了出来。
    """
    query = select(UserCharacterTemplate).where(UserCharacterTemplate.user_id == user_id)
    if system_id is not None:
        query = query.where(UserCharacterTemplate.system_id == system_id)
    rows = (
        (await db.execute(query.order_by(UserCharacterTemplate.updated_at.desc()))).scalars().all()
    )
    return [_to_template_read(row) for row in rows]


async def count_character_templates(db: AsyncSession, user_id: str) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(UserCharacterTemplate)
            .where(UserCharacterTemplate.user_id == user_id)
        )
    ) or 0


async def create_character_template(
    db: AsyncSession, user_id: str, payload: CharacterTemplateCreateBody
) -> CharacterTemplateRead:
    """把一张已建好的角色卡存成常用卡。

    只收 `character_id`：`system_id` 与建卡态都由后端从那张卡（和它所在的
    房间）读出来，前端不参与决定"存什么"。

    🔴 **只有新存那条路数上限**，`overwrite_character_template`（更新已有那张）
    不数——它不产生新行，卡在上限的人否则连改都改不了。
    """
    character = await db.get(Character, payload.character_id)
    if character is None:
        raise CharacterNotFoundError("角色不存在")
    room = await db.get(Room, character.room_id)
    if room is None or room.system_id is None:
        raise CharacterNotFoundError("这张角色卡不属于任何已选定规则系统的房间")
    if await count_character_templates(db, user_id) >= TEMPLATE_LIMIT:
        raise CharacterTemplateLimitReachedError(f"卡库最多存 {TEMPLATE_LIMIT} 张，删掉一些再存吧")

    template = UserCharacterTemplate(
        user_id=user_id,
        system_id=room.system_id,
        name=payload.name,
        data=character_to_template_data(character),
    )
    db.add(template)
    await db.commit()
    await db.refresh(template)
    return _to_template_read(template)


async def get_character_template(
    db: AsyncSession, user_id: str, template_id: str
) -> CharacterTemplateRead:
    return _to_template_read(await _require_template(db, user_id, template_id))


async def overwrite_character_template(
    db: AsyncSession, user_id: str, template_id: str, payload: CharacterTemplateOverwriteBody
) -> CharacterTemplateRead:
    """把一张角色卡的当前建卡态**整份写回**卡库里已有的那张。

    场景：玩家用常用卡开了一局，在向导里改了改，觉得这就是这个调查员的正式
    版本——他要的是"更新那张"，不是"再存一张"。

    🔴 **跟 PATCH 的分工**：那条是详情页改文字，只收白名单里的文本字段；这条
    连属性技能一起覆盖，但数据**由后端从角色卡读**（同 `create_character_template`
    的那段），前端一个规则数都碰不到。区别不在"改多少"，在"数据谁给的"。

    🔴 卡名不动：卡库里的名字是玩家自己起的（「跑长期的那张」），不该被角色名
    盖掉。要改名走详情页。
    """
    template = await _require_template(db, user_id, template_id)
    character = await db.get(Character, payload.character_id)
    if character is None:
        raise CharacterNotFoundError("角色不存在")
    room = await db.get(Room, character.room_id)
    if room is None or room.system_id is None:
        raise CharacterNotFoundError("这张角色卡不属于任何已选定规则系统的房间")
    if room.system_id != template.system_id:
        raise CharacterTemplateNotFoundError("这张角色卡的规则系统跟那张常用卡对不上")

    template.data = character_to_template_data(character)
    await db.commit()
    await db.refresh(template)
    return _to_template_read(template)


#: 卡库详情页能改的建卡态字段。🔴 **规则相关的数一个都不收**：属性、年龄、职业、
#: 技能、生成方式、衍生值改一处就要重跑整套 COC7 校验与年龄修正，而那套链路已经
#: 长在建卡向导那条路上——在卡库里再造一套，就是"功能写在某个实现里，换一个实现
#: 就悄悄没了"。想改数值的路是：用这张卡开局 → 在向导里改 → 再存一张。
_TEMPLATE_EDITABLE_FIELDS = (
    "name",
    "gender",
    "residence",
    "birthplace",
    # 装备是一串物品名（`Character.equipment` 是 list[str]），跟备注一样属于
    # 文字：COC7 不按装备算数值，改它不触发任何规则计算。
    "equipment",
    "background",
    "notes",
    "background_detail",
)


async def update_character_template(
    db: AsyncSession, user_id: str, template_id: str, payload: CharacterTemplateUpdateBody
) -> CharacterTemplateRead:
    """改卡库里那张卡的文字部分（卡名 + 建卡态里的文本字段）。

    `data` 是**部分更新**：只合并请求里真正给了的键，没给的原样留着——整份覆盖
    的话，前端少传一个字段就等于把它清空了。
    """
    template = await _require_template(db, user_id, template_id)
    if payload.name is not None:
        template.name = payload.name
    if payload.data:
        unknown = sorted(set(payload.data) - set(_TEMPLATE_EDITABLE_FIELDS))
        if unknown:
            # 显式拒绝而不是静默丢弃：静默丢弃的话，前端以为改上去了、界面上也
            # 显示改了，刷新一下又变回去——这种 bug 两头都不会变红。
            raise CharacterTemplateNotEditableError(f"这些字段不能在卡库里改：{'、'.join(unknown)}")
        merged = {**(template.data or {}), **payload.data}
        # 🔴 `background_detail` 是**字典的字典**，顶层合并对它不够：前端只认识
        # 自己那八栏，整份替换会把模板里别的键静默抹掉（以后加栏、或别处写进去
        # 的东西）。嵌一层合并——键的含义是前端表单的事，后端不逐键校验，但也
        # 不替它删。
        incoming_detail = payload.data.get("background_detail")
        if isinstance(incoming_detail, dict):
            existing_detail = (template.data or {}).get("background_detail")
            merged["background_detail"] = {
                **(existing_detail if isinstance(existing_detail, dict) else {}),
                **incoming_detail,
            }
        template.data = merged
    await db.commit()
    await db.refresh(template)
    return _to_template_read(template)


async def delete_character_template(db: AsyncSession, user_id: str, template_id: str) -> None:
    """删掉卡库里那张。

    🔴 **先把引用它的角色卡指针清掉**：`characters.based_on_template_id` 有外键，
    但 SQLite 默认不强制，删完那些角色卡就指向一个不存在的模板。指针的消费方是
    准备页的「在卡库」——于是那张卡永远显示"已经在卡库里了"，而卡库里根本没有，
    玩家再也存不进去（2026-08-13 扫描复现）。**悬空的指针比没有指针更坏**：它
    让下游做出确信的错误判断。
    """
    template = await _require_template(db, user_id, template_id)
    await db.execute(
        update(Character)
        .where(Character.based_on_template_id == template.id)
        .values(based_on_template_id=None)
    )
    await db.delete(template)
    await db.commit()


async def _require_template(
    db: AsyncSession, user_id: str, template_id: str
) -> UserCharacterTemplate:
    """按 id 取模板，**同时校验它属于这个账号**。

    🔴 不能只按 id 查：模板 id 是 uuid，但"猜不到"不是访问控制。别人的卡库
    是别人的。
    """
    template = await db.get(UserCharacterTemplate, template_id)
    if template is None or template.user_id != user_id:
        raise CharacterTemplateNotFoundError("常用卡不存在")
    return template


def _to_template_read(template: UserCharacterTemplate) -> CharacterTemplateRead:
    return CharacterTemplateRead(
        template_id=template.id,
        name=template.name,
        system_id=template.system_id,
        data=template.data or {},
        created_at=template.created_at,
        updated_at=template.updated_at,
    )
