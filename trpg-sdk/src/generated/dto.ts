/**
 * 本文件由 `npm run codegen` 从后端 pydantic 模型自动生成，请勿手改。
 *
 * 源头：trpg-backend/app/dto/{auth,room,character,common,ws}.py
 * 重新生成：
 *   1. cd trpg-backend && uv run python scripts/export_schema.py
 *   2. cd trpg-sdk && npm run codegen
 * 生成后把这个文件的改动一并提交——CI 会重新跑一遍上面两步，用 git diff
 * 校验有没有人改了后端 DTO 却忘记重新生成（issue #75 决策 3）。
 */

/**
 * action.broadcast 推送 payload（issue #107）——玩家对 AI 主持人说的
 * **原话**的房间广播。
 *
 * 修的是三人联机实测出的"聊天记录像被隔离"bug：此前玩家原话只在发送方
 * 本地插入气泡，其他人只能从守秘人回复的转述里看到内容。现在 action.submit
 * 先广播这条（谁、说了什么），再广播 AI 的叙事回复（narration.push），
 * 所有人看到的时间线一致——就像牌桌上说话大家都听得见。
 */
export interface ActionBroadcastPayload {
  playerId: string;
  nickname: string;
  utterance: string;
  eventId?: string | null;
}

/**
 * action.submit 事件 payload——issue #107 定稿后玩家对 AI 主持人说的
 * **唯一**事件（行动或提问都走它，"是哪种"由 AI 判断，协议层不预分类）。
 *
 * `utterance` 必填，理由同 PlayerReadyPayload.ready：一条不带行动内容的
 * action.submit 是畸形消息。给默认空串会让 SDK 侧变成 `utterance?: string`，
 * 于是 `submitAction(playerId, {})` 类型检查通过、运行时静默无操作。
 *
 * 注意「必填」只管字段存在，空白内容（`""` / `"   "`）仍由下游的
 * `strip()` + 空值判断拦掉，两者不冲突。
 *
 * `summarized_from` / `visibility` 是 issue #107 铺的协议位：
 * - `summarized_from`：这次提交是从讨论区哪几条消息总结来的（消息 id 列表）。
 *   本期只透传不消费——AI 编排（#48/#68）接手后决定怎么用。
 * - `visibility`：`"private"` 表示「我偷偷摸他口袋」这类不想让其他玩家看到
 *   结果的私密行动。本期传 `private` 会收到 NOT_IMPLEMENTED 的 error——
 *   真正的私密裁决需要 AI 知道「结果只给发起者且后续叙事不能泄露」，属于
 *   编排层的活，硬做会做出一个会漏信息的假私密，比不做更糟（issue #107
 *   关键决策）。**不静默当成 public 处理**：把玩家以为保密的行动广播出去，
 *   当场就暴露了。
 */
export interface ActionSubmitPayload {
  utterance: string;
  summarizedFrom?: string[] | null;
  visibility?: ("public" | "private") | null;
}

/**
 * POST /api/v1/rooms/{roomId}/characters/{characterId}/apply-age-adjustment
 * 请求体。
 */
export interface AgeAdjustmentRequest {
  age: number;
}

/**
 * apply-age-adjustment 的响应：调整前后的完整属性 + 每一步的掷骰/减值
 * 明细，供前端展示"发生了什么"而不只是甩最终数字。
 */
export interface AgeAdjustmentResult {
  age: number;
  ageLabel: string;
  attributesBefore: {
    [k: string]: number;
  };
  attributesAfter: {
    [k: string]: number;
  };
  eduChecks?: EduImprovementCheckView[];
  eduFlatAdjustment?: number;
  scdLoss?: number;
  scdAffectedAttributes?: string[];
  appLoss?: number;
  luckRerolled?: boolean;
  movPenalty?: number;
}

/**
 * 调查员年龄的合法区间（issue #96）。
 *
 * COC7 的年龄档从 15-19 起、到 80-89 止，所以合法区间是 [15, 89]。此前前端
 * 的输入框写死成 [10, 100]，两头都不符合规则。
 *
 * 注意本期只做区间约束，**不做年龄修正**（15-19 岁扣 STR/SIZ/EDU 各若干、
 * 20-39 岁一次教育增强检定、40 岁起每十年 MOV -1 等）——那是一整套生成期
 * 规则，要单独做。
 */
export interface AgeRangeSpec {
  minValue: number;
  maxValue: number;
}

/**
 * 加一个 AI 队友（exec/21）。三个字段都可选。
 *
 * `seed` 用于可复现——同一个 seed 造出同一张卡，测试与试玩装置需要它。
 */
export interface AiPlayerCreateBody {
  nickname?: string | null;
  occupation?: string | null;
  seed?: number | null;
}

/**
 * 点数购买法的约束（issue #96）。
 *
 * 这些数字此前只存在于前端代码里、后端既不校验也不暴露，导致 ①任何 SDK
 * 使用者都能提交 UI 永远不允许的角色卡 ②重写前端时必须把规则再实现一遍。
 * 放进 ruleset 是为了「一份定义、两方消费」：后端拿它裁决，客户端拿它渲染
 * 「还剩多少点」「这项最多加到多少」。
 *
 * 只约束 `point_buy=True` 的属性；幸运不在其列。
 */
export interface AttributePointBuyRules {
  budget: number;
  minValue: number;
  maxValue: number;
  defaultValue: number;
}

/**
 * 掷点池法里的一次骰子明细：`kind` 是骰子公式（`3d6x5`/`2d6+6x5`），
 * `dice` 是原始骰子值，`value` 是这一项换算后的最终点数。
 */
export interface AttributePoolRollView {
  kind: string;
  dice: number[];
  value: number;
}

/**
 * 一项基础属性：键名、显示名、COC7 生成公式。
 *
 * `point_buy` 表示这一项是否参与点数购买法的分配。COC7 里幸运只能掷
 * （`3d6*5`）、不能用属性点买，所以它是 `False`——客户端据此决定哪些属性
 * 渲染成可加点、哪些只读展示，不需要自己维护一份"哪 8 项能加点"的名单
 * （issue #96：这份名单此前在前端硬编码了三处，加幸运时漏改一处导致
 * 角色卡看不到幸运值）。
 */
export interface AttributeSpec {
  key: string;
  label: string;
  generation: string;
  pointBuy?: boolean;
}

/**
 * 注册 / 登录成功后的返回：登录凭证 + 用户信息。
 */
export interface AuthResult {
  token: string;
  userId: string;
  nickname: string;
}

/**
 * POST /api/v1/auth/password 请求体。
 *
 * 要旧密码：改密码这个动作的意义就在于"只有本人能改"，而 token 可能是从一台
 * 没锁屏的机器上拿到的。
 */
export interface ChangePasswordBody {
  oldPassword: string;
  newPassword: string;
}

/**
 * POST /rooms/{roomId}/characters/{characterId}/complete 请求体（可选）。
 *
 * `equipment_notes`：物品名 → 玩家对它的说明（「我父亲留下的，他是一战老兵」）。
 * 这是**申辩那一步**——真人桌上"这个人哪来的枪"不是主持人单方面判定，而是
 * 玩家给个理由、主持人点头。第一版没有它，实测 1925 年图书管理员带把左轮被
 * 稳定拦下 3/3。
 *
 * 🔴 **不落库**：它是玩家对守秘人说的一句解释，不是卡面数据，只影响这一次
 * 校验。存起来就要多一列、多一次迁移，而且下一次校验该不该沿用上一次的
 * 说辞本身就是个新问题。
 */
export interface CharacterCompleteBody {
  equipmentNotes?: {
    [k: string]: string;
  };
}

/**
 * `compute_preview` 的响应结构：衍生值 + 两个技能点预算 + 全部技能的
 * base/cap/当前值 + 校验报告。
 */
export interface CharacterComputeResult {
  derivedStats: {
    [k: string]: number | string;
  };
  occupationSkillPoints: SkillPointsBudgetView;
  interestSkillPoints: SkillPointsBudgetView;
  skillView: SkillComputeView[];
  validation: ValidationIssueView[];
  slotOccupiedSkillIds?: string[];
}

/**
 * POST /api/v1/rooms/{roomId}/characters 返回
 */
export interface CharacterDraftResult {
  characterId: string;
  status: string;
}

/**
 * `character.may_have_changed` 推送：这一拍结束了，自己那张卡重拉一次。
 *
 * 🔴 **名字是故意这么起的**：它**不声称任何东西真的变了**，只声称"现在是
 * 重读的安全点"。有一个字段说"谁变了"就必须有人算得出来，而算不出来——
 * 改角色卡的路径散在 `write_stat`（HP/SAN）、幸运消费、`inventory` 的装备
 * 增删里，各写各的。
 *
 * ## 为什么需要它（2026-08-16 真机）
 *
 * 前端「什么时候重拉角色卡」原来是**逐个列出**的两处：`san.check.result`
 * 一处、`character.stat_changed`（HP）一处。于是：
 *
 * - **幸运消费**没人配 → 玩家花掉 3 点，界面上那个数字一局不变；
 * - **装备变化**（第十片能力，前一天刚加的）也没人配 → 同样不变。
 *
 * 「加一项就漏一项」，而且两头都不会变红：后端扣得好好的，前端渲染得也好好
 * 的。所以修的是**形状**不是那两个分支——挂进 `_push_after_turn` 这个唯一的
 * 回合结束出口，以后任何一片能力改了角色卡都自动覆盖，不用记得配一条 if。
 *
 * 代价是每回合每人多一次 `GET .../characters/{id}`。一个回合本来就是十几秒
 * 级的多次 LLM 往返，这一次读可以忽略；换来的是**不会再漏第三样**。
 */
export interface CharacterMayHaveChangedPayload {}

/**
 * POST /api/v1/systems/{systemId}/character/preview 请求体。
 */
export interface CharacterPreviewRequest {
  attributes: {
    [k: string]: number;
  };
  occupationId?: number | null;
  skills?: {
    [k: string]: number;
  };
  age?: number | null;
  generationMethod?: string | null;
  attributePoolTotal?: number | null;
  allocatedAttributes?: {
    [k: string]: number;
  } | null;
}

/**
 * GET /api/v1/rooms/{roomId}/characters/{characterId} 返回（issue #96）。
 *
 * 补这个端点是为了让**后端成为角色卡的唯一事实来源**。此前只有
 * 创建/保存/完成/掷属性四个写操作、没有任何读接口，前端因此只能把角色卡
 * 存进 localStorage 当权威源——而那份副本的结构会随后端 schema 演进而过期
 * （PR #88 加幸运后，旧的 8 键角色卡就再也编辑不了了）。
 *
 * `generation_method` 一并返回：客户端要据此知道这张卡该按点数购买法还是
 * 掷骰法来渲染与校验。
 */
export interface CharacterRead {
  id: string;
  status: string;
  generationMethod: string;
  attributePoolTotal?: number | null;
  name?: string | null;
  age?: number | null;
  gender?: string | null;
  residence?: string;
  birthplace?: string;
  attributes?: {
    [k: string]: number;
  };
  allocatedAttributes?: {
    [k: string]: number;
  } | null;
  derivedStats?: {
    [k: string]: number | string;
  };
  hpMax?: number | null;
  skills?: {
    [k: string]: number;
  };
  equipment?: string[];
  occupationId?: number | null;
  occupation?: string | null;
  background?: string;
  notes?: string;
  backgroundDetail?: {
    [k: string]: string;
  } | null;
  basedOnTemplateId?: string | null;
}

/**
 * character.stat_changed 推送 payload（feat/keeper-agent，真人实测 09-#4
 * 修复）——HP 变更的结构化广播。
 *
 * San 已经有 `san.check.result` 携带 `san_remaining`（走"检定→掷骰→广播
 * 结果"这条路），不需要这个事件；HP 变化是裁决直接判定伤害后立即执行，
 * 没有对应的检定/掷骰事件可以携带新值，此前只把结果拼进叙事正文当纯文本，
 * 前端角色卡拿不到任何结构化数据、HP 从进房间起就是建卡快照、永不更新。
 */
export interface CharacterStatChangedPayload {
  playerId: string;
  hp: number;
  hpMax?: number | null;
  reason?: string | null;
}

/**
 * POST /api/v1/me/character-templates 请求体：把一张已建好的卡存成常用卡。
 *
 * 🔴 **只收 `character_id`，不收 `data`。** 原先的形状是让前端把建卡态自己
 * 拼好传上来——那等于把"什么算建卡态"这条规则挪到前端（规则权威在后端），
 * 而且 `Character` 加一列就要两边同时改、漏一边不会变红。现在后端自己去读
 * 那张卡；`system_id` 同理，从卡所在的房间读。
 */
export interface CharacterTemplateCreateBody {
  name: string;
  characterId: string;
}

/**
 * PUT /api/v1/me/character-templates/{templateId} 请求体：拿一张角色卡的
 * 当前状态整份覆盖卡库里那张。
 *
 * 只收 `character_id`，理由同 `CharacterTemplateCreateBody`：存什么由后端决定。
 * 卡名不在里面——卡库里的名字是玩家起的，不该被角色名盖掉（要改名走 PATCH）。
 */
export interface CharacterTemplateOverwriteBody {
  characterId: string;
}

/**
 * `我的常用角色卡` 列表/详情返回项。
 */
export interface CharacterTemplateRead {
  templateId: string;
  name: string;
  systemId: string;
  data: {
    [k: string]: unknown;
  };
  createdAt: string;
  updatedAt: string;
}

/**
 * PATCH /api/v1/me/character-templates/{templateId} 请求体：改卡库里那张卡。
 *
 * 🔴 **只改文字，不改规则数**：`data` 里能给的键由 service 层的白名单说了算
 * （姓名/性别/居住地/出生地/背景故事/备注/结构化背景），属性、年龄、职业、
 * 技能一律不收——那些改一处就要重跑整套 COC7 校验与年龄修正，而那条链路长在
 * 建卡向导上，在卡库里再造一套等于同一件事有两个实现。
 *
 * 两个字段都可选，且 `data` 是**部分更新**：只合并真正给了的键。
 */
export interface CharacterTemplateUpdateBody {
  name?: string | null;
  data?: {
    [k: string]: unknown;
  } | null;
}

/**
 * PATCH /api/v1/rooms/{roomId}/characters/{characterId} 请求体
 */
export interface CharacterUpdateBody {
  name: string;
  age?: number | null;
  gender?: string | null;
  residence?: string;
  birthplace?: string;
  attributes: {
    [k: string]: number;
  };
  allocatedAttributes?: {
    [k: string]: number;
  } | null;
  derivedStats: {
    [k: string]: number;
  };
  skills: {
    [k: string]: number;
  };
  equipment?: EquipmentItem[];
  occupationId?: number | null;
  occupation?: string | null;
  background?: string;
  notes?: string;
  backgroundDetail?: {
    [k: string]: string;
  } | null;
  generationMethod?: string | null;
}

/**
 * chat.message 推送 payload（issue #107）——讨论区消息的房间广播。
 *
 * 带 `client_message_id` 回传是为了让发送方把广播和自己本地乐观插入的
 * 那条对上号（去重/替换本地占位），其他人直接按新消息渲染。
 * `sent_at` 用 UtcDatetime：所有对外时间字段必须带时区后缀，否则客户端
 * 会把 UTC 当本地时间解析（UTC+8 上「4 分钟前」显示成「8 小时前」的真 bug）。
 */
export interface ChatMessagePayload {
  messageId: string;
  playerId: string;
  nickname: string;
  text: string;
  sentAt: string;
  clientMessageId: string;
}

/**
 * 讨论区一条消息。`sent_at` 用 UtcDatetime（对外时间字段的统一约定，
 * 见 app/dto/common.py）。
 */
export interface ChatMessageRead {
  messageId: string;
  playerId: string;
  nickname: string;
  text: string;
  sentAt: string;
  clientMessageId: string;
}

/**
 * chat.send 事件 payload（issue #107）——玩家往**讨论区**发一条消息。
 *
 * 讨论区跟「对 AI 主持人说话」（action.submit）是两条完全独立的通道：
 * 讨论区消息只在玩家之间广播，**永远不进任何 LLM 上下文**（成本 + 玩家
 * 需要"AI 听不见"的商量空间，这是 #107 的立项理由）。
 *
 * `client_message_id` 是客户端生成的去重键：断线重连后客户端可能重发同一条
 * 消息，服务端靠 `(player_id, client_message_id)` 唯一约束保证只落一行、
 * 重发拿到与第一次一致的广播。
 */
export interface ChatSendPayload {
  text: string;
  clientMessageId: string;
}

/**
 * check.request 推送 payload（issue #77 新增；feat/keeper-agent 起在
 * keeper 模式下真的会发出——守秘人裁决需要检定后，不立即掷骰，而是随叙事
 * 一起推这条通知，玩家在前端点击「掷骰」确认后才真正生成骰值）。
 *
 * `check_request_id` 是这次待掷检定的标识，玩家确认时原样带回
 * （`check.roll`/`san.check.roll` 的 payload）。非 keeper 模式（Fallback/
 * DeepSeekNarrator）不会发出这个事件。
 */
export interface CheckRequestPayload {
  playerId: string;
  skill: string;
  targetValue?: number | null;
  checkRequestId: string;
  reason?: string | null;
}

/**
 * check.result 推送 payload（issue #77 新增；feat/keeper-agent 起真的
 * 会发出）。
 *
 * 直接返回终值，不做两段式初步结果（issue 决策 4：幸运消耗机制推迟，
 * 协议一并简化）——这里的"两段式"指的是幸运消耗，不要和"两段式玩家掷骰"
 * （裁决/掷骰分离）混淆。
 */
export interface CheckResultPayload {
  playerId: string;
  skill: string;
  rollValue: number;
  targetValue?: number | null;
  result: string;
  checkRequestId?: string | null;
  opposedOpponent?: string | null;
  opposedRollValue?: number | null;
  opposedTargetValue?: number | null;
  opposedResult?: string | null;
  opposedWon?: boolean | null;
  opposedVerdict?: string | null;
  effectiveRollValue?: number | null;
  luckSpent?: number | null;
}

/**
 * check.roll 事件 payload（issue #77 新增，feat/keeper-agent 两段式玩家
 * 掷骰实现）——玩家确认并结算一次守秘人已发起的待掷检定。
 *
 * `check_request_id` 必填：标识具体是哪一次待掷检定（守秘人裁决"需要
 * 检定"后随叙事一起广播的 `check.request` 事件带的那个 id）。骰值由服务端
 * 权威生成——这条消息本身不带任何"掷什么/掷多少"的信息，纯粹是"我确认
 * 掷这一个"。
 *
 * ## `roll_value`：玩家自己掷的那颗实体骰（`exec/46` B5）
 *
 * **默认不带 = 服务端权威掷**，与本字段上线前逐字一致。
 *
 * 带了它表示"我用桌上的骰子掷了，出目是这个"——**只有开了
 * `rooms.allow_manual_rolls` 的房间才收**，没开的房间收到会被**明确拒绝**，
 * 不静默忽略（静默忽略等于玩家报了个数、系统偷偷用了别的数）。
 *
 * 🔴 **1–100 的范围校验放在这里**：它是 d100，报 0 或 101 不是"作弊"而是
 * "这不是一颗 d100 能掷出来的数"。真作弊（报一个对自己有利的合法数）在
 * 「私有部署、自己和朋友玩」的定位下是社交问题不是技术问题——线下桌上报假
 * 数字比在软件里改数字容易得多。
 */
export interface CheckRollPayload {
  checkRequestId: string;
  rollValue?: number | null;
}

/**
 * clue.granted 推送 payload（issue #77 新增，线索发现，本期不会真的发出）。
 */
export interface ClueGrantedPayload {
  playerId: string;
  clueName: string;
  description?: string | null;
}

/**
 * 一次 EDU 改进检定的掷骰明细：服务端权威 `d100`，`roll > eduBefore`
 * 才算成功，成功再掷 `1d10` 当增量（上限 99）。
 */
export interface EduImprovementCheckView {
  success: boolean;
  roll: number;
  gain: number;
  eduBefore: number;
  eduAfter: number;
}

/**
 * `game.end.decide` 客户端事件：同意收工，或者不。
 *
 * 有"不同意"这个动作（同 `luck.decide`、不同于会合确认）：不表态就是维持
 * 默认，而这里的默认方向是**继续玩**——所以拒绝必须能被明确说出来，它会
 * 当场清掉整批卡。
 */
export interface EndGameDecidePayload {
  decisionId: string;
  accepted: boolean;
}

/**
 * `game.end.request` 推送：有人提议结束这一局，请你表态（2026-08-19）。
 *
 * 真人线下团里收尾最高频的入口是**玩家自己宣布**的（「我们报警，然后回家」），
 * 而在这之前玩家手上只有否决权和确认权、没有发起权。这张卡是那条入口。
 *
 * 🔴 **全票才结束、一票否决**：「结束」跟掷骰不同，它作用于整桌人。
 * 也**没有超时自动同意**——超时自动会把「没看见这张卡」变成「同意结束」，
 * 而这一步之后是硬墙。
 */
export interface EndGameRequestPayload {
  decisionId: string;
  playerId: string;
  initiator: string;
}

/**
 * `game.end.status` 推送：这次提议的进展。
 *
 * 三种终局：还在等谁（`waiting_for` 非空）、被谁否了（`declined_by`）、
 * 全票通过（`finished`，此时 `keeper.phase` 会另发一条 `finished`）。
 */
export interface EndGameStatusPayload {
  waitingFor: string[];
  declinedBy: string | null;
  finished: boolean;
}

/**
 * POST /rooms/{roomId}/characters/{characterId}/check-equipment 请求体。
 *
 * 🔴 **判断素材由前端带上，不从库里读**：向导直到最后一步才 PATCH，而这次
 * 校验发生在**离开装备那一步**的时候——那时库里那张卡还是空的（真人反馈
 * 2026-08-19：「应该在装备那个界面点击下一步就该有」）。
 *
 * `era` 不在这里：它来自模组，属于服务端权威，让客户端传等于把"这一局是
 * 哪个年代"交给客户端说了算。
 */
export interface EquipmentCheckBody {
  equipment?: string[];
  occupation?: string | null;
  age?: number | null;
  residence?: string | null;
  birthplace?: string | null;
  creditRating?: number | null;
  notes?: {
    [k: string]: string;
  };
}

/**
 * 哪几件这个人拿不到。空列表 = 全都合理，可以进下一步。
 *
 * 🔴 `checked=False` 表示**这次判断没跑成**（没配 key / 超时 / JSON 崩了），
 * 跟"全都合理"完全是两回事：前者放行但不该让玩家以为审过了。同
 * `EquipmentChecker.check` 返回 None 的那条判据。
 */
export interface EquipmentCheckResult {
  checked: boolean;
  rejected: RejectedEquipmentView[];
}

export interface EquipmentItem {
  name: string;
}

/**
 * 统一错误码枚举。
 *
 * 用 StrEnum（Python 3.11+）而不是普通字符串常量或 int 枚举，好处是：
 * - 序列化成 JSON 时直接是字符串值（比如 "NOT_FOUND"），前端/SDK 拿到的就是可读的码；
 * - 类型检查器（ty/mypy）能校验到哪些地方在用错误码，重命名/新增时不会漏改；
 * - 每个成员名本身就是 UPPER_SNAKE_CASE，跟成员值保持一致，一眼能看出对应关系。
 *
 * 新增错误码时，在这里加一行即可；用哪个 HTTP 状态码由抛出方（业务代码里的
 * AppException(...) 调用）决定，这个枚举本身不绑定状态码。
 */
export type ErrorCode =
  | "VALIDATION_ERROR"
  | "BAD_REQUEST"
  | "UNAUTHORIZED"
  | "FORBIDDEN"
  | "NOT_FOUND"
  | "CONFLICT"
  | "INTERNAL_ERROR"
  | "ROOM_NOT_FOUND"
  | "ROOM_FULL"
  | "MODULE_VALIDATION_FAILED"
  | "NOT_YOUR_TURN"
  | "ACTION_IN_PROGRESS"
  | "CHARACTER_INCOMPLETE"
  | "MODULE_NOT_SELECTED"
  | "RATE_LIMITED"
  | "NOT_IMPLEMENTED"
  | "CHARACTER_INVALID"
  | "RULESET_NOT_CONFIGURED"
  | "CHECK_NOT_PENDING"
  | "ATTRIBUTES_NOT_SET"
  | "ALREADY_ROLLED";

/**
 * 错误信息的具体内容，只在 success=false 时出现在 error 字段里。
 *
 * `details` 是 issue #84 S2 新增的可选字段：装结构化的校验报告（比如建卡
 * 校验失败时的一条条 {code, field, message}），大多数错误不需要它，默认
 * None，不影响原有只有 code/message 的错误响应形状。
 */
export interface ErrorDetail {
  code: ErrorCode;
  message: string;
  details?:
    | {
        [k: string]: string;
      }[]
    | null;
}

/**
 * error 推送 payload（issue #77 新增）。
 *
 * 发起者做不成的事要明说，不能静默丢弃（`continue`，见 ws.py 旧逻辑）让
 * 客户端干等。非 keeper 叙事实现下的 `check.roll`/`san.check.roll` 也走它
 * 回 NOT_IMPLEMENTED。
 */
export interface ErrorPayload {
  code: string;
  message: string;
}

/**
 * game.ended 推送 payload（issue #77 新增，触发复盘，本期不会真的发出）。
 */
export interface GameEndedPayload {
  reason?: string | null;
}

/**
 * 游戏大类。
 */
export interface GameRead {
  id: string;
  name: string;
  description?: string | null;
}

/**
 * game.start 事件 payload——目前不带任何字段。
 *
 * 定义一个空模型（而不是完全跳过校验）是为了让 game.start 也走跟其它事件
 * 一致的"接收端过一次模型校验"路径，行为对齐、不搞特例。
 */
export interface GameStartPayload {}

/**
 * 大类下的规则系统。
 */
export interface GameSystemRead {
  id: string;
  gameId: string;
  name: string;
  version?: string | null;
}

/**
 * POST /api/v1/rooms/{roomCode}/join 请求体
 */
export interface JoinRoomBody {
  nickname?: string | null;
}

/**
 * `keeper.busy` 推送：守秘人正在别处忙（`exec/33 §5.4`）。
 *
 * 分头时叙事是逐组生成的，没轮到的那一组屏幕上此前**什么都没有**，静默十几秒
 * 然后突然弹出一段。线下你至少看得见 KP 在跟别人说话。
 */
export interface KeeperBusyPayload {
  busy: boolean;
}

/**
 * `keeper.phase` 推送：这一局走到哪一步了（2026-08-15）。
 *
 * 🔴 **补的是一条只有一半的链**：`closure` 早就会把 phase 写成 `ending` /
 * `finished`，叙事纪律与字数上限也照着它变，**但前端一个字都收不到**。
 * 玩家侧的表现是：说完「结束了吧」，收到一段普通叙事，界面毫无变化——
 * 「整条链都在，就是没人能用到」的又一处。
 *
 * ⚠️ 别跟 `RoomPhasePayload` 搞混：那个是**大厅级**的房间状态
 * （Lobby/InGame/Completed），而且从来没有地方发出过。这一条是**对局内**
 * 的守秘人阶段（opening/investigation/ending/finished），两者粒度不同。
 *
 * 只发 phase 与 ending_id，不发别的——收尾门里的数字（还剩几条配对没揭开）
 * 是守秘人的判断依据，给玩家看等于剧透进度条。
 */
export interface KeeperPhasePayload {
  phase: string;
  endingId: string | null;
}

/**
 * GET /api/v1/rooms/{roomId}/last-session 返回：「上次讲到哪」（`exec/46` B3）。
 *
 * 🔴 `recap_text` **必填但可为 null**，不给默认值：服务端每次都送得出这个
 * 字段，`null` 的含义是「这一局还没散过会 / 那一场什么都没发生 / 没配 key」
 * ——三种都是如实降级。给了默认值生成的 TS 就是可选的，前端只能 `?? ''`，
 * 而那正好把「没有」和「没送」混成一件事（这个仓库一天踩过两次）。
 */
export interface LastSessionRead {
  sessionCount: number;
  recapText: string | null;
  adjourned: boolean;
}

/**
 * POST /api/v1/auth/login 请求体
 */
export interface LoginBody {
  account: string;
  password: string;
}

/**
 * `luck.decide` 客户端事件：花，或者不花。
 *
 * 🔴 跟会合确认不同，这里**有"不花"这个动作**：会合不点就是维持分离（安全
 * 方向就是默认），而这里不答一句，那次检定的结果就一直悬着——整轮停在那儿。
 */
export interface LuckDecidePayload {
  decisionId: string;
  accepted: boolean;
}

/**
 * `luck.offer` 推送：骰子已经停下，问他要不要花幸运把失败推成成功
 * （`exec/26 #66`）。
 *
 * **卡片本身就是教学位**——新手根本不知道有这条规则，只有主持人知道。所以
 * 差几点、花多少、剩多少全都写出来，而不是只给一个「消耗幸运」按钮。
 */
export interface LuckOfferPayload {
  decisionId: string;
  playerId: string;
  skill: string;
  rolled: number;
  target: number;
  cost: number;
  luckRemaining: number;
  opposedOpponent?: string | null;
}

/**
 * 临时性疯狂的一种发作表现（COC7「疯狂发作·即时」1D10 表）。
 *
 * 有 id 才有地基：此前"单次损失≥5 触发临时疯狂"只是掷骰文本末尾的一句
 * 警告，症状由叙事器现编，下一轮没有任何地方记着它——同「即兴出来的东西
 * 没有落点」。做成 id 之后它是 keeper_state 里的一条记录、局面块里的一行，
 * 解除必须走裁决字段。
 *
 * `roll` 是它在 1D10 表上的点数（服务端掷，模型碰不到）。表本身属于规则
 * 系统而不是引擎：COC7 是插件，症状表跟着它走。
 */
export interface MadnessSymptomSpec {
  id: string;
  roll: number;
  label: string;
  description: string;
}

/**
 * GET /PATCH /api/v1/auth/me 返回
 */
export interface MeRead {
  userId: string;
  account: string;
  nickname: string;
}

/**
 * GET /api/v1/modules/{moduleId} 返回——列表字段 + 玩家可见前情。
 *
 * - synopsis：目录简介（Scenario 表，选模组用）
 * - player_intro / opening_script：来自 structured JSON 的玩家可见开场
 *   （绝不含 kp_truth；文件缺失时为 null）
 * - story_pages：前端前情页直接渲染的段落列表（intro + opening 去重）
 */
export interface ModuleDetailRead {
  id: string;
  title: string;
  version: string;
  authors: string[];
  playersMin: number;
  playersMax: number;
  difficulty: number;
  estimatedDuration?: string | null;
  isImported?: boolean;
  createdAt?: string | null;
  synopsis?: string | null;
  playerIntro?: string | null;
  openingScript?: string | null;
  storyPages?: string[];
}

/**
 * POST /api/v1/modules/import 与 GET /api/v1/modules/import/{jobId} 返回。
 *
 * 不用 `from_attributes` 直接从 ORM 对象转换——ORM 主键列叫 `id`，这里
 * 对外字段叫 `job_id`（避免跟其它 DTO 的 `xxxId` 命名约定不一致），两者
 * 对不上，构造时由 service 层显式传关键字参数更直接。
 *
 * 🔴 **这个 DTO 是剧透约束的最后一道关**（`exec/29 §2`）。导入的人就是即将
 * 开玩的玩家，所以跨到前端的**只有数量与拓扑**——没有节点标题、没有 NPC 名字、
 * **连生成的实体 id 都没有**（id 是从内容里长出来的）。失败原因只给封闭集合里
 * 的类别词（`job_state.FAILURE_KINDS`），不是错误原文——原文里带着 id、数值和
 * 半句正文。
 *
 * 加字段前先回答：**它能不能装下一句剧透？** 能就别加。
 */
export interface ModuleImportJobRead {
  jobId: string;
  status: string;
  stage: string;
  sourceFilename?: string | null;
  resultScenarioId?: string | null;
  errorMessage?: string | null;
  failureKinds: string[];
  pageCount: number;
  imageCount: number;
  charCount: number;
  itemCount: number;
  nodeCount: number;
  npcCount: number;
  endingCount: number;
  agendaCount: number;
  hardFailureCount: number;
  retriedFromJobId?: string | null;
  createdAt: string;
  updatedAt: string;
  finishedAt?: string | null;
}

/**
 * 模组信息（对应内容库 `Scenario` 表，`from_attributes=True` 支持直接从
 * ORM 对象构造）。
 */
export interface ModuleRead {
  id: string;
  title: string;
  version: string;
  authors: string[];
  playersMin: number;
  playersMax: number;
  difficulty: number;
  estimatedDuration?: string | null;
  isImported?: boolean;
  createdAt?: string | null;
}

/**
 * GET /api/v1/me/rooms 返回项
 */
export interface MyRoomSummary {
  roomId: string;
  roomCode: string;
  roomName: string;
  phase: string;
  moduleId?: string | null;
  moduleTitle?: string | null;
  playerCount: number;
  maxPlayers: number;
  updatedAt: string;
  isHost?: boolean;
}

/**
 * narration.delta 推送 payload（`exec/28`）——叙事流式到达的一段。
 *
 * 🔴 **它不是新的事实来源。** `events` 表仍然只落一行完整叙事，
 * `GET /rooms/{roomId}/replay` 一行不用改；delta 纯粹是实时通道的加速。
 * 重连的人拿 replay 的完整文本，**不重放流式**——刷新页面后把整局叙事重打
 * 一遍，玩家会疯（`exec/26 #62` 第一条要求）。
 *
 * 每段都已经过完纪律层与泄密守门才发出（`runtime/narration_stream`）：
 * **推出去的字不可撤回**，所以守门必须在推之前，不能在之后。
 */
export interface NarrationDeltaPayload {
  eventId: string;
  seq: number;
  text: string;
  private?: boolean;
}

/**
 * narration.push 推送 payload。
 */
export interface NarrationPushPayload {
  text: string;
  private?: boolean;
  eventId?: string | null;
}

/**
 * 一个职业：信用评级区间、职业技能点公式、职业技能清单。
 *
 * 职业技能 = `skill_ids`（固定）+ `choice_slots`（自选，见 `SkillChoiceSlot`）。
 * 两者都吃职业技能点；其余技能吃兴趣点。
 */
export interface OccupationSpec {
  id: number;
  name: string;
  category: string;
  creditMin: number;
  creditMax: number;
  skillPointsFormula: string;
  skillIds: string[];
  choiceSlots?: SkillChoiceSlot[];
  description: string;
}

/**
 * GET /api/v1/rooms/{roomId}/characters 里的一张队友卡（exec/14 P5.3）。
 *
 * ## 为什么是"放开"而不是"收紧"
 *
 * P5.2 那一批（分头探索/潜行/私密行动）都在收紧可见性，这条方向相反：
 * 真人桌上角色卡是摊在桌面、互相传阅的，队友当然知道你几点力量、会不会
 * 开锁——此前系统里**只有「读回自己那张」**，反而比真人桌更封闭。
 *
 * exec/18 已逐条裁决 ⑦检定过程与结果、⑧HP/SAN 一律公开，所以这里
 * **不做任何脱敏**：属性、衍生值、技能、装备、背景全给。
 *
 * 与 `CharacterRead` 的差别只在两头：
 * - **去掉**建卡过程字段（`generation_method` / `attribute_pool_total` /
 *   `allocated_attributes`）——那是"这张卡怎么捏出来的"，只有卡主本人的
 *   建卡向导需要，队友看了没有意义，给出去还多一份能被误用的权威数字；
 * - **加上** `player_id` / `nickname`——队友卡面板要能说清"这张是谁的"，
 *   角色名和玩家昵称是两回事。
 */
export interface PartyCharacterRead {
  playerId: string;
  nickname: string;
  id: string;
  status: string;
  name?: string | null;
  age?: number | null;
  gender?: string | null;
  residence?: string;
  birthplace?: string;
  attributes?: {
    [k: string]: number;
  };
  derivedStats?: {
    [k: string]: number | string;
  };
  skills?: {
    [k: string]: number;
  };
  equipment?: string[];
  occupationId?: number | null;
  occupation?: string | null;
  background?: string;
  backgroundDetail?: {
    [k: string]: string;
  } | null;
}

/**
 * `party.merge.confirm` 客户端事件：当事人确认「我确实跟他们碰上了」。
 *
 * 没有对应的"否认"动作——不确认就是维持分离，那本来就是默认与安全方向。
 */
export interface PartyMergeConfirmPayload {}

/**
 * `party.update` 推送：这个玩家自己的空间处境（`exec/33 §5.4`）。
 *
 * 🔴 **逐人裁过再发**，不是把全房间的分组表广播出去：别处那一组在哪、有谁，
 * 对你的角色而言是不该知道的（他们可能还在潜行）。所以这里只有
 * 「我在哪 · 谁跟我在一处 · 另有几组人在别处」——**够玩家看出系统把他放错了
 * 地方，又不泄露内容**。
 *
 * 它存在的理由：真人实测里系统把队友拖进了地下室，而**界面上一处都没有位置
 * 信息**，于是没有任何人会发现。装上这只眼睛之后，静默错误变成可见错误。
 */
export interface PartyUpdatePayload {
  locationId: string | null;
  locationName: string | null;
  companions: string[];
  otherGroups: number;
  mergePendingAt: string | null;
}

/**
 * POST /api/v1/rooms/{roomId}/players/{playerId}/away 请求体。
 *
 * 显式的 `away` 而不是两个动词端点（`/away` 与 `/back`）：**这是一个开关，
 * 不是两件事**，两个端点会让"他到底在不在"多出一处需要同步的判断。
 */
export interface PlayerAwayBody {
  away: boolean;
}

/**
 * player.joined 推送 payload（issue #77 新增，同上，本期不会真的发出）。
 */
export interface PlayerJoinedPayload {
  player: RoomPlayerRead;
}

/**
 * player.ready 事件 payload。
 *
 * `ready` 必填、不给默认值：协议上「设置准备状态」这个动作必须说清楚要设成
 * 什么，缺字段是一条畸形消息，应该被丢弃，而不是被悄悄当成 `False` 处理。
 * 这里给默认值的代价不只在后端——它会顺着 codegen 变成 SDK 的
 * `ready?: boolean`，让 `setReady(playerId, {})` 也能通过类型检查并静默地把
 * 玩家设成未准备（见 PR #76 review）。改动前的手写 SDK 类型本来就是必填的。
 */
export interface PlayerReadyPayload {
  ready: boolean;
}

/**
 * POST /api/v1/rooms/{roomId}/characters/quick-build 请求体。
 *
 * 只要一个名字。属性/职业/技能全部由服务端随机生成（与 AI 队友同一个
 * 生成器），玩家不需要理解任何 COC7 规则就能开局。
 */
export interface QuickBuildCharacterBody {
  name: string;
}

/**
 * POST /api/v1/auth/register 请求体
 */
export interface RegisterBody {
  account: string;
  password: string;
  nickname: string;
}

/**
 * 一件判为「拿不到」的装备。
 */
export interface RejectedEquipmentView {
  item: string;
  message: string;
}

/**
 * GET /api/v1/rooms/{roomId}/replay 返回项——对应 `events` 表的一行。
 */
export interface ReplayEventRead {
  id: string;
  playerId?: string | null;
  eventType: string;
  payload: {
    [k: string]: unknown;
  };
  createdAt: string;
}

/**
 * POST /api/v1/rooms/{roomId}/characters/{characterId}/roll-attribute-pool
 * 返回：8 次掷骰明细 + 求和后的总点数池。分配到八维由玩家在前端完成，
 * 最终结果走 PATCH 保存——`total` 就是 `complete` 时校验分配总和用的权威值
 * （`character.attribute_pool_total`）。
 */
export interface RollAttributePoolResult {
  rolls: AttributePoolRollView[];
  total: number;
}

/**
 * POST /api/v1/rooms/{roomId}/characters/{characterId}/roll-attributes 返回。
 *
 * 服务端权威掷骰（COC7 标准法）：STR/CON/DEX/APP/POW = 3d6*5，
 * SIZ/INT/EDU = (2d6+6)*5；衍生值按标准公式算出 HP/MP/SAN，写回
 * `characters.attributes`/`derived_stats` 后原样返回给客户端展示。
 */
export interface RollAttributesResult {
  attributes: {
    [k: string]: number;
  };
  derivedStats: {
    [k: string]: number;
  };
}

/**
 * POST /api/v1/rooms/{roomId}/characters/{characterId}/roll-luck 返回：
 * 单独掷一次幸运（3d6×5），不占八维分配的预算。`kind`/`dice` 命名口径对齐
 * `AttributePoolRollView`。服务端把 `value` 写进
 * `character.attributes["LUCK"]`（只改这一个键，不动其它属性、不动
 * `generation_method`）。
 *
 * 15–19 岁的"幸运掷两次取高"由 `apply-age-adjustment` 负责（`luckRerolled`
 * 字段），这个端点不判年龄，避免同一条规则两处实现。
 */
export interface RollLuckResult {
  kind: string;
  dice: number[];
  value: number;
}

/**
 * `room.adjourn` 客户端事件：今晚到此为止 / 下次接着跑（`exec/46` B3）。
 *
 * 🔴 跟 `room.pause` **是两档粒度，不是一件事**：休息是几分钟、任何人都能
 * 按、什么都不生成；散会是几天、只有房主能按、要留下「上次讲到哪」。
 * 一个 bool 的理由同 `RoomPausePayload`。
 */
export interface RoomAdjournPayload {
  adjourned: boolean;
}

/**
 * `room.adjourned` 推送：今晚收工了 / 又开始了。
 *
 * `session_count` 是「这一局聚过几次」——收工那一刻正好是它 +1 的时候，
 * 顺手带上省一次查询。
 */
export interface RoomAdjournedPayload {
  adjourned: boolean;
  byNickname: string;
  sessionCount: number;
  recapText: string | null;
}

/**
 * POST /api/v1/rooms 请求体
 */
export interface RoomCreate {
  nickname?: string | null;
  roomName: string;
  maxPlayers?: number;
}

/**
 * POST /api/v1/rooms 返回
 */
export interface RoomCreateResult {
  roomId: string;
  roomCode: string;
  reconnectToken: string;
  playerId: string;
  isHost: boolean;
  characterId?: string | null;
}

/**
 * room.join 事件 payload。
 *
 * `reconnect_token` 必填：它是玩家在这个房间里的身份密钥（`players.reconnect_token`，
 * 建房/加入时下发给本人）。WS 连接握手只校验了「你是某个登录账号」，但连接
 * 时带的 playerId 是任意的、而且被公开房间预览暴露——只认 playerId 会让任何
 * 登录用户绑定成别人（冒充房主 game.start / 提交行动，PR #78 review 指出）。
 * 绑定时要求出示该玩家的 reconnect_token，才能证明「你就是这个玩家本人」。
 *
 * roomCode/nickname 是前端沿用原型习惯发送的冗余字段，服务端不读，保留可选
 * 以免影响现有调用方。
 */
export interface RoomJoinPayload {
  reconnectToken: string;
  roomCode?: string | null;
  nickname?: string | null;
}

/**
 * `room.pause` 客户端事件：暂停 / 恢复（`exec/35`）。
 *
 * 一个事件带 bool，而不是 pause/resume 两个事件——「暂停中」是个状态位，
 * 两个事件会让"连点两次暂停"和"没暂停就恢复"各自需要一条规则。
 */
export interface RoomPausePayload {
  paused: boolean;
}

/**
 * `room.paused` 推送：房间暂停状态变了，附带是谁按的。
 */
export interface RoomPausedPayload {
  paused: boolean;
  byNickname: string;
}

/**
 * 房间内玩家摘要。
 *
 * 注意 `player_id` 对应 ORM `Player` 的主键属性 `id`（名字不一样），所以不能直接
 * `model_validate(player_orm)`——调用方需要显式映射 `player_id=p.id`（见
 * service/room.py 的 _to_room_preview）。`from_attributes=True` 仍保留，方便
 * 其余名字一致的字段。camelCase 别名生成、populate_by_name 继承自 `CamelModel`——
 * pydantic 的 `model_config` 在子类里是合并而非整体覆盖父类配置，这里不需要
 * 重复声明（issue #77 审计发现 #1，原先这里重写了一份和父类一样的配置，是
 * #75 遗留的死代码）。
 */
export interface RoomPlayerRead {
  playerId: string;
  nickname: string;
  isHost: boolean;
  ready: boolean;
  hasCharacter: boolean;
  isAi?: boolean;
}

/**
 * GET /api/v1/rooms/{roomCode} 返回
 */
export interface RoomPreview {
  roomId: string;
  roomCode: string;
  roomName: string;
  phase: string;
  storyStarted: boolean;
  moduleId?: string | null;
  moduleTitle?: string | null;
  playerCount: number;
  maxPlayers: number;
  allowManualRolls: boolean;
  players: RoomPlayerRead[];
}

/**
 * PATCH /api/v1/rooms/{roomId} 请求体。
 *
 * 人数上限 + 「骰子在桌上」。房间名不在这里：改名是纯展示需求，而这条接口
 * 的存在理由是"位置不够了"这个会卡住桌子的问题——两件事没必要绑在一起。
 * 区间跟建房时一致（`RoomCreate.max_players`），下界由服务层再按当前人数
 * 收紧一次（不能调到比在座的人还少）。
 *
 * 🔴 `allow_manual_rolls` **可选**：不传 = 不动它。这条接口原本只改人数，
 * 把它做成必填会让所有既有调用方（前端那一处、e2e）在不知情的情况下把开关
 * 重置成 False——**加字段时给已有调用方留原样不动的那条路**。
 */
export interface RoomSettingsBody {
  maxPlayers: number;
  allowManualRolls?: boolean | null;
}

/**
 * room.state 推送 payload（issue #77 新增，替代 HTTP 轮询伪广播）。
 *
 * 本期协议槽位已留好（信封类型/校验器/SDK 方法齐全），但 ws.py 里没有任何
 * 地方会真的发出这个事件——大厅玩家列表仍然是前端 `GET /rooms/{roomCode}`
 * 轮询获取（issue"三处原型取舍"表格，真正切换依赖前端改动，本期不动
 * trpg-frontend）。
 */
export interface RoomStatePayload {
  roomId: string;
  phase: string;
  players: RoomPlayerRead[];
}

/**
 * GET /api/v1/rooms/{roomId}/summary 返回。
 */
export interface RoomSummaryRead {
  roomId: string;
  summaryText?: string | null;
  highlights?: string[] | null;
  missedTruths?: string[] | null;
}

/**
 * 建卡所需的规则数据：属性/技能/职业目录（`GET /systems/{systemId}/ruleset`）。
 */
export interface RulesetRead {
  attributes: AttributeSpec[];
  attributePointBuy?: AttributePointBuyRules | null;
  ageRange?: AgeRangeSpec | null;
  skills: SkillSpec[];
  occupations: OccupationSpec[];
  successTiers?: SuccessTierSpec[];
  madnessSymptoms?: MadnessSymptomSpec[];
}

/**
 * san.check.request 推送 payload（issue #77 新增；feat/keeper-agent 起
 * 真的会发出，同 CheckRequestPayload 的理智检定版本）。
 */
export interface SanCheckRequestPayload {
  playerId: string;
  currentSan?: number | null;
  checkRequestId: string;
  reason?: string | null;
}

/**
 * san.check.result 推送 payload（issue #77 新增，同 CheckResultPayload
 * 直接返回终值；feat/keeper-agent 起真的会发出）。
 */
export interface SanCheckResultPayload {
  playerId: string;
  rollValue: number;
  sanLoss: number;
  result: string;
  checkRequestId?: string | null;
  sanRemaining?: number | null;
}

/**
 * san.check.roll 事件 payload（issue #77 新增，feat/keeper-agent 两段式
 * 玩家掷骰实现）——同 CheckRollPayload，理智检定版本。
 */
export interface SanCheckRollPayload {
  checkRequestId: string;
}

/**
 * POST /api/v1/rooms/{roomId}/module 请求体
 */
export interface SelectModuleBody {
  moduleId: string;
  attributeGenMethod?: string;
}

/**
 * session.bound 推送 payload。
 */
export interface SessionBoundPayload {
  roomId: string;
  playerId: string;
}

/**
 * 职业技能里的一个「自选槽」：从 `candidate_skill_ids` 里选 `count` 项，
 * 选中的技能算**职业技能**（吃职业技能点），而不是兴趣技能（issue #114）。
 *
 * COC7 的职业技能不是一份固定清单，而是「固定技能 + N 个自选槽」，例如
 * 私家侦探是「技艺（摄影），乔装，法律，图书馆，**一项社交技能**（取悦、
 * 话术、恐吓、说服），心理学，侦查，**任意一项**其他个人或时代特长」。
 * 229 个职业里有 221 个（96.5%）至少带一个槽。
 *
 * 此前数据模型只有固定 `skill_ids`，装不下槽，于是"一项社交技能（四选一）"
 * 只能被压平成固定两项——这正是现有 30 个职业技能列表失真的原因（全部被
 * 规整成恰好 8 项，而规则书里实际是 0–15 项），并且会**误杀合法角色卡**：
 * 玩家把点数加在规则书认可、但被压平时丢掉的本职技能上，会被当成兴趣技能
 * 计费而触发 `INTEREST_POINTS_EXCEEDED`。
 *
 * `candidate_skill_ids` 为 `None` 表示**任意技能**（规则书里的"任意 N 项
 * 其他个人或时代特长"）；给出列表则表示限定候选集（如社交技能四选一）。
 */
export interface SkillChoiceSlot {
  count: number;
  candidateSkillIds?: string[] | null;
  label: string;
}

/**
 * 一项技能的计算结果：基础值/已分配点数/当前值/上限。
 */
export interface SkillComputeView {
  id: string;
  base: number;
  allocated: number;
  current: number;
  cap: number;
}

/**
 * 一个技能点池（职业/兴趣）的预算/已用/剩余。
 */
export interface SkillPointsBudgetView {
  budget: number;
  spent: number;
  remaining: number;
}

/**
 * 一项技能：基础值可以是固定数字，也可以是依赖属性的公式字符串
 * （比如闪避 `DEX/2`、母语 `EDU`）。
 */
export interface SkillSpec {
  id: string;
  name: string;
  nameEn?: string | null;
  base: number | string;
  category: string;
  relatedAttr?: string | null;
}

/**
 * 一档比"成功"更严的成功等级：门槛 = 技能值 ÷ `divisor`（向下取整）。
 *
 * COC7 里是困难（÷2）和极难（÷5）。声明成数据而不是让客户端写死除数，是因为
 * **同一份除数还要用于服务端判定**（`keeper/primitives/dice.py`）——角色卡上
 * 那三格展示与真正的裁决必须同源，否则改规则时必漏一处。
 *
 * 没声明这一项的规则系统（非 COC7 的自定义系统）就是没有这个概念，客户端
 * 什么都不该画——那不是兜底，是如实反映"这套规则里没有难度分档"。
 */
export interface SuccessTierSpec {
  id: string;
  label: string;
  divisor: number;
}

/**
 * POST /api/v1/rooms/{roomId}/host 请求体——把房主交给谁。
 */
export interface TransferHostBody {
  playerId: string;
}

/**
 * turn.begin 推送 payload（issue #77 新增，回合制约束，本期不会真的发出）。
 */
export interface TurnBeginPayload {
  playerId: string;
}

/**
 * `turn.clarify` 客户端事件：「你把我的话理解错了」（`exec/35`）。
 *
 * `clarification` 必填，理由同 `ActionSubmitPayload.utterance`：不带内容的
 * 纠错是畸形消息，而给默认空串会让 SDK 侧变成可选、于是静默无操作。
 *
 * 🔴 **没有"纠正哪一轮"这个参数**：只能纠最新的一轮。翻旧账要能定位到
 * 任意一轮的世界状态，那是一整套 undo 基础设施；而且真人桌上纠错本来就
 * 只发生在刚刚那一拍。
 */
export interface TurnClarifyPayload {
  clarification: string;
}

/**
 * PATCH /api/v1/auth/me 请求体
 */
export interface UpdateNicknameBody {
  nickname: string;
}

/**
 * 一条结构化校验失败信息，空列表代表这张卡合法。
 */
export interface ValidationIssueView {
  code: string;
  field: string;
  message: string;
}

/**
 * view.private 推送 payload（issue #77 新增，私密视角/不泄底的载体）。
 *
 * 本期协议槽位已留好，但 `narration.push` 仍然是全房间广播（issue
 * "三处原型取舍"表格），没有任何地方会真的发出这个事件——真正的信息
 * 不对称需要规则引擎知道"这条叙事该给谁看"，归 #48/#68。
 */
export interface ViewPrivatePayload {
  playerId: string;
  text: string;
}
