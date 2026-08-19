"""角色（调查员）建卡（issue #59）的 pydantic 请求/响应模型。

建卡流程分两段：POST 创建草稿 → PATCH 保存完整数据 → POST complete 标记完成，
跟 trpg-app 原型（character-api.ts）的四步向导一一对应：信息/属性/技能三步
在前端本地完成，第四步"完成"时把整份角色数据一次性 PATCH 上来。属性/衍生值/
技能的具体数值仍由客户端整体提交、后端负责校验形状、持久化，以及把
RoomPlayer.has_character 标记为 True；但 issue #84 S2 起，`complete_character`
落库前会用 `app/core/coc7/rules.py` 权威重算并校验一遍（职业/兴趣技能点预算、
技能上限、信用评级区间等），不合法直接拒绝——不再只信任客户端算好的数值。
建卡过程中的实时预览走本文件下方的 `CharacterPreviewRequest`/
`CharacterComputeResult`（`POST /systems/{systemId}/character/preview`）。
"""

from pydantic import Field

from app.dto.common import CamelModel, UtcDatetime


class EquipmentItem(CamelModel):
    name: str = Field(..., min_length=1, max_length=200)


class CharacterUpdateBody(CamelModel):
    """PATCH /api/v1/rooms/{roomId}/characters/{characterId} 请求体"""

    name: str = Field(..., min_length=1, max_length=100)
    age: int | None = None
    gender: str | None = Field(default=None, max_length=20)
    residence: str = Field(default="", max_length=100)
    birthplace: str = Field(default="", max_length=100)
    attributes: dict[str, int]
    # 玩家分配的原始属性（年龄修正之前），语义见 `character.
    # allocated_attributes`（wizard-bugfix-round4，方案 A）。可选：不传就是
    # 没有这份数据（比如还没走到年龄步骤，或不关心年龄修正的调用方）。
    allocated_attributes: dict[str, int] | None = None
    derived_stats: dict[str, int]
    skills: dict[str, int]
    equipment: list[EquipmentItem] = Field(default_factory=list)
    # 🔴 职业用 id 定位（exec/22）：职业名不唯一，规则表里有 6 组同名不同项的
    # 职业，信用区间乃至技能点公式都不同。前端在预览步骤本来就拿着 id
    # （`CharacterPreviewRequest.occupation_id`），保存时也要把它带过来——
    # 信息此前正是在这一步丢的。
    occupation_id: int | None = None
    # 展示名。id 缺失时（老客户端）回退按名字查，行为与改动前一致。
    occupation: str | None = None
    background: str = Field(default="", max_length=4000)
    notes: str = Field(default="", max_length=4000)
    # 结构化背景故事（迁移自 coc-char-gen）：personalDescription/ideology/
    # significantPeople/meaningfulLocations/treasuredPossessions/traits/
    # injuries/phobias 8 个引导字段，值可以是空字符串。不做逐键校验——键的
    # 含义是前端表单的事，后端只透明存取。
    background_detail: dict[str, str] | None = None
    # 前端"点数购买"按钮想显式告诉后端"我不再用掷骰/掷点池那份属性了"
    # （否则玩家切回点数购买法重新分配，complete 时仍会被掷点池的总和精确
    # 匹配校验拦下，见 character-build-migration 已知缺口）。这里只信任
    # "降级到点数购买"这一个方向——点数购买本身就是校验最严的模式（硬预算
    # 上限+逐项范围），声明切到它不可能被用来绕过任何约束；服务层不接受
    # 客户端借这个字段自称 roll/roll_pool（那两个来源标记只能由服务端掷骰
    # 接口写入，见 PR #97 review [1] 堵住的漏洞），此字段传别的值一律忽略。
    generation_method: str | None = None


class CharacterRead(CamelModel):
    """GET /api/v1/rooms/{roomId}/characters/{characterId} 返回（issue #96）。

    补这个端点是为了让**后端成为角色卡的唯一事实来源**。此前只有
    创建/保存/完成/掷属性四个写操作、没有任何读接口，前端因此只能把角色卡
    存进 localStorage 当权威源——而那份副本的结构会随后端 schema 演进而过期
    （PR #88 加幸运后，旧的 8 键角色卡就再也编辑不了了）。

    `generation_method` 一并返回：客户端要据此知道这张卡该按点数购买法还是
    掷骰法来渲染与校验。
    """

    id: str
    status: str
    generation_method: str
    # 掷点池法（roll_pool）掷出的权威总值，其余两种生成方法恒为 None——前端
    # 编辑一张已保存的掷点池角色卡时要靠这个字段精确恢复预算分母，不能退而
    # 求其次拿"当前已保存的八维总和"去猜（那个数字在玩家后续手动改动过属性
    # 后会跟真正掷出来的池子值不一致）。
    attribute_pool_total: int | None = None
    name: str | None = None
    age: int | None = None
    gender: str | None = None
    residence: str = ""
    birthplace: str = ""
    attributes: dict[str, int] = Field(default_factory=dict)
    # 玩家分配的原始属性（年龄修正之前，wizard-bugfix-round4 方案 A）——编辑
    # 已有角色卡时前端要靠它恢复"玩家原本分配了多少"，不能拿套用过年龄修正
    # 的 `attributes` 去猜。本列之前建的卡没有这份数据，恒为 `None`。
    allocated_attributes: dict[str, int] | None = None
    derived_stats: dict[str, int | str] = Field(default_factory=dict)
    # HP 上限（血条的分母，`exec/26` #67）。`derived_stats["HP"]` 在被守秘人
    # 改过之后是**当前值**，客户端拿它当分母会让带伤的角色显示成满血；上限
    # 由后端按 `resolve_max_hp` 权威给出。还没有属性的草稿卡为 `None`。
    hp_max: int | None = None
    skills: dict[str, int] = Field(default_factory=dict)
    equipment: list[str] = Field(default_factory=list)
    occupation_id: int | None = None
    occupation: str | None = None
    background: str = ""
    notes: str = ""
    background_detail: dict[str, str] | None = None
    # 这张卡是从卡库里哪张常用卡复制来的（没用常用卡建的恒为 None）。客户端据此
    # 知道"它已经在卡库里了"——否则准备页会请玩家把同一张卡再存一遍，卡库里就
    # 多出一张一模一样的（2026-08-13 真人反馈）。
    based_on_template_id: str | None = None


class PartyCharacterRead(CamelModel):
    """GET /api/v1/rooms/{roomId}/characters 里的一张队友卡（exec/14 P5.3）。

    ## 为什么是"放开"而不是"收紧"

    P5.2 那一批（分头探索/潜行/私密行动）都在收紧可见性，这条方向相反：
    真人桌上角色卡是摊在桌面、互相传阅的，队友当然知道你几点力量、会不会
    开锁——此前系统里**只有「读回自己那张」**，反而比真人桌更封闭。

    exec/18 已逐条裁决 ⑦检定过程与结果、⑧HP/SAN 一律公开，所以这里
    **不做任何脱敏**：属性、衍生值、技能、装备、背景全给。

    与 `CharacterRead` 的差别只在两头：
    - **去掉**建卡过程字段（`generation_method` / `attribute_pool_total` /
      `allocated_attributes`）——那是"这张卡怎么捏出来的"，只有卡主本人的
      建卡向导需要，队友看了没有意义，给出去还多一份能被误用的权威数字；
    - **加上** `player_id` / `nickname`——队友卡面板要能说清"这张是谁的"，
      角色名和玩家昵称是两回事。
    """

    player_id: str
    nickname: str
    id: str
    status: str
    name: str | None = None
    age: int | None = None
    gender: str | None = None
    residence: str = ""
    birthplace: str = ""
    attributes: dict[str, int] = Field(default_factory=dict)
    derived_stats: dict[str, int | str] = Field(default_factory=dict)
    skills: dict[str, int] = Field(default_factory=dict)
    equipment: list[str] = Field(default_factory=list)
    occupation_id: int | None = None
    occupation: str | None = None
    background: str = ""
    background_detail: dict[str, str] | None = None


class CharacterCreateBody(CamelModel):
    """POST /api/v1/rooms/{roomId}/characters 请求体（issue #77 新增第三条建卡路径）。

    整个请求体本身仍然可选（不传等价于从零建卡，路由层用 `Body(default=None)`
    兜底），`based_on_template_id` 指向 `user_character_templates` 表——本期
    只接住这个参数、校验它的形状，真正"复制模板数据进草稿"的读写没有实现
    （issue 决策 5：本期只铺表与接口），带了这个字段会直接收到 NOT_IMPLEMENTED。
    """

    based_on_template_id: str | None = Field(default=None, min_length=1)


class QuickBuildCharacterBody(CamelModel):
    """POST /api/v1/rooms/{roomId}/characters/quick-build 请求体。

    只要一个名字。属性/职业/技能全部由服务端随机生成（与 AI 队友同一个
    生成器），玩家不需要理解任何 COC7 规则就能开局。
    """

    name: str = Field(min_length=1, max_length=50)


class CharacterDraftResult(CamelModel):
    """POST /api/v1/rooms/{roomId}/characters 返回"""

    character_id: str
    status: str


class RollAttributesResult(CamelModel):
    """POST /api/v1/rooms/{roomId}/characters/{characterId}/roll-attributes 返回。

    服务端权威掷骰（COC7 标准法）：STR/CON/DEX/APP/POW = 3d6*5，
    SIZ/INT/EDU = (2d6+6)*5；衍生值按标准公式算出 HP/MP/SAN，写回
    `characters.attributes`/`derived_stats` 后原样返回给客户端展示。
    """

    attributes: dict[str, int]
    derived_stats: dict[str, int]


class CharacterTemplateCreateBody(CamelModel):
    """POST /api/v1/me/character-templates 请求体：把一张已建好的卡存成常用卡。

    🔴 **只收 `character_id`，不收 `data`。** 原先的形状是让前端把建卡态自己
    拼好传上来——那等于把"什么算建卡态"这条规则挪到前端（规则权威在后端），
    而且 `Character` 加一列就要两边同时改、漏一边不会变红。现在后端自己去读
    那张卡；`system_id` 同理，从卡所在的房间读。
    """

    name: str = Field(..., min_length=1, max_length=200)
    character_id: str = Field(..., min_length=1)


class CharacterTemplateOverwriteBody(CamelModel):
    """PUT /api/v1/me/character-templates/{templateId} 请求体：拿一张角色卡的
    当前状态整份覆盖卡库里那张。

    只收 `character_id`，理由同 `CharacterTemplateCreateBody`：存什么由后端决定。
    卡名不在里面——卡库里的名字是玩家起的，不该被角色名盖掉（要改名走 PATCH）。
    """

    character_id: str = Field(..., min_length=1)


class CharacterTemplateUpdateBody(CamelModel):
    """PATCH /api/v1/me/character-templates/{templateId} 请求体：改卡库里那张卡。

    🔴 **只改文字，不改规则数**：`data` 里能给的键由 service 层的白名单说了算
    （姓名/性别/居住地/出生地/背景故事/备注/结构化背景），属性、年龄、职业、
    技能一律不收——那些改一处就要重跑整套 COC7 校验与年龄修正，而那条链路长在
    建卡向导上，在卡库里再造一套等于同一件事有两个实现。

    两个字段都可选，且 `data` 是**部分更新**：只合并真正给了的键。
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    data: dict[str, object] | None = None


class CharacterTemplateRead(CamelModel):
    """`我的常用角色卡` 列表/详情返回项。"""

    template_id: str
    name: str
    system_id: str
    data: dict
    created_at: UtcDatetime
    updated_at: UtcDatetime


# ── 建卡计算/校验预览（issue #84 S2，路线乙的接缝） ────────────────────────
#
# 前端建卡过程中把当前草稿（属性/职业/技能分配）发给
# `POST /api/v1/systems/{systemId}/character/preview`，后端用
# `app/core/coc7/rules.py` 权威算出全部派生量 + 校验报告，前端只负责渲染，
# 不再本地重算 COC7 规则数值——`complete_character` 最终落库前也是复用同一套
# 计算/校验，两处结果不会不一致。


class CharacterPreviewRequest(CamelModel):
    """POST /api/v1/systems/{systemId}/character/preview 请求体。"""

    attributes: dict[str, int]
    occupation_id: int | None = None
    skills: dict[str, int] = Field(default_factory=dict)
    # 可选：不传就是"年龄未知/尚未走到年龄步骤"，衍生值按不扣年龄惩罚算
    # （向 compute_derived_stats 传 None，这是它本来的默认行为）。传了就用
    # 同一份权威公式扣 MOV，让建卡向导里的实时预览和 complete 时最终落库
    # 的衍生值一致——此前只有 complete 传 age，预览页面的 MOV 一直没扣，见
    # character-build-migration 已知缺口。
    age: int | None = None
    # 可选：不传就退回默认的点数购买法校验（`compute_preview` 的默认行为）。
    # 掷点池/服务端掷骰玩家的实时预览必须传各自的 `generation_method`，否则
    # 后端永远按点数购买法（预算 480）校验属性总和——见 wizard-bugfix-round1
    # 核心发现，此前这两个字段完全不存在，是预览衍生值/技能点预算整体退化成
    # 空的根因。
    generation_method: str | None = None
    # 可选：`generation_method="roll_pool"` 时的权威总值（`character.
    # attribute_pool_total`），语义同 `validate_character` 的同名参数——只有
    # 这条路径会用到，其余生成方法传了也会被忽略。
    attribute_pool_total: int | None = None
    # 可选：玩家分配的原始属性（年龄修正之前），语义见 `character.
    # allocated_attributes`（wizard-bugfix-round4，方案 A）。传了之后
    # `attributes` 只承担计算职责（衍生值/技能基础值/职业技能点公式），生成
    # 方法约束（预算/池值总和/步进为 5）改校验这份分配值；不传就退回旧行为，
    # `attributes` 一身二任。
    allocated_attributes: dict[str, int] | None = None


class SkillPointsBudgetView(CamelModel):
    """一个技能点池（职业/兴趣）的预算/已用/剩余。"""

    budget: int
    spent: int
    remaining: int


class SkillComputeView(CamelModel):
    """一项技能的计算结果：基础值/已分配点数/当前值/上限。"""

    id: str
    base: int
    allocated: int
    current: int
    cap: int


class CharacterCompleteBody(CamelModel):
    """POST /rooms/{roomId}/characters/{characterId}/complete 请求体（可选）。

    `equipment_notes`：物品名 → 玩家对它的说明（「我父亲留下的，他是一战老兵」）。
    这是**申辩那一步**——真人桌上"这个人哪来的枪"不是主持人单方面判定，而是
    玩家给个理由、主持人点头。第一版没有它，实测 1925 年图书管理员带把左轮被
    稳定拦下 3/3。

    🔴 **不落库**：它是玩家对守秘人说的一句解释，不是卡面数据，只影响这一次
    校验。存起来就要多一列、多一次迁移，而且下一次校验该不该沿用上一次的
    说辞本身就是个新问题。
    """

    equipment_notes: dict[str, str] = Field(default_factory=dict)


class ValidationIssueView(CamelModel):
    """一条结构化校验失败信息，空列表代表这张卡合法。"""

    code: str
    field: str
    message: str


class CharacterComputeResult(CamelModel):
    """`compute_preview` 的响应结构：衍生值 + 两个技能点预算 + 全部技能的
    base/cap/当前值 + 校验报告。"""

    derived_stats: dict[str, int | str]
    occupation_skill_points: SkillPointsBudgetView
    interest_skill_points: SkillPointsBudgetView
    skill_view: list[SkillComputeView]
    validation: list[ValidationIssueView]
    # 哪些非固定本职技能占用了职业自选槽（character-build-migration
    # redesign-v2 §4-B）：只读展示字段，前端用它在编辑已保存的卡时重建
    # ★ 列表、渲染"占槽"徽标，不参与任何校验语义，值来自
    # `coc7_rules.ComputeResult.slot_occupied_skill_ids`。
    slot_occupied_skill_ids: list[str] = Field(default_factory=list)


# ── 年龄调整（迁移自 coc-char-gen `js/plugins/age.js`） ─────────────────────


class AgeAdjustmentRequest(CamelModel):
    """POST /api/v1/rooms/{roomId}/characters/{characterId}/apply-age-adjustment
    请求体。"""

    age: int = Field(..., ge=1, le=120)


class EduImprovementCheckView(CamelModel):
    """一次 EDU 改进检定的掷骰明细：服务端权威 `d100`，`roll > eduBefore`
    才算成功，成功再掷 `1d10` 当增量（上限 99）。"""

    success: bool
    roll: int
    gain: int
    edu_before: int
    edu_after: int


class AgeAdjustmentResult(CamelModel):
    """apply-age-adjustment 的响应：调整前后的完整属性 + 每一步的掷骰/减值
    明细，供前端展示"发生了什么"而不只是甩最终数字。"""

    age: int
    age_label: str
    attributes_before: dict[str, int]
    attributes_after: dict[str, int]
    edu_checks: list[EduImprovementCheckView] = Field(default_factory=list)
    edu_flat_adjustment: int = 0
    scd_loss: int = 0
    scd_affected_attributes: list[str] = Field(default_factory=list)
    app_loss: int = 0
    luck_rerolled: bool = False
    mov_penalty: int = 0


# ── 掷点池生成法（迁移自 coc-char-gen `js/core/dice.js::rollAttributePointPool`） ──


class AttributePoolRollView(CamelModel):
    """掷点池法里的一次骰子明细：`kind` 是骰子公式（`3d6x5`/`2d6+6x5`），
    `dice` 是原始骰子值，`value` 是这一项换算后的最终点数。"""

    kind: str
    dice: list[int]
    value: int


class RollAttributePoolResult(CamelModel):
    """POST /api/v1/rooms/{roomId}/characters/{characterId}/roll-attribute-pool
    返回：8 次掷骰明细 + 求和后的总点数池。分配到八维由玩家在前端完成，
    最终结果走 PATCH 保存——`total` 就是 `complete` 时校验分配总和用的权威值
    （`character.attribute_pool_total`）。"""

    rolls: list[AttributePoolRollView]
    total: int


# ── 幸运单掷（character-build-migration redesign-v2 §4-A） ──────────────────


class RollLuckResult(CamelModel):
    """POST /api/v1/rooms/{roomId}/characters/{characterId}/roll-luck 返回：
    单独掷一次幸运（3d6×5），不占八维分配的预算。`kind`/`dice` 命名口径对齐
    `AttributePoolRollView`。服务端把 `value` 写进
    `character.attributes["LUCK"]`（只改这一个键，不动其它属性、不动
    `generation_method`）。

    15–19 岁的"幸运掷两次取高"由 `apply-age-adjustment` 负责（`luckRerolled`
    字段），这个端点不判年龄，避免同一条规则两处实现。"""

    kind: str
    dice: list[int]
    value: int
