"""WebSocket 事件 payload 的 pydantic 模型（issue #75）。

在这之前，`/ws/{roomId}`（app/controller/ws.py）的 6 个事件全部是手搓的裸
dict——发送端直接 `send_json({"type": ..., "payload": {...}})`，接收端直接
`payload.get("ready")` / `payload.get("utterance")`。这意味着"把 Pydantic
模型导出成 JSON Schema 再生成 TS 类型"这条管线对 WS 完全无从谈起：没有模型
可导。这个文件把现有 6 个事件的 payload 补成真正的 Pydantic 模型，ws.py
也相应改成用这些模型收发（不再靠 .get() 兜底），管线才能覆盖到 WS。

跟 dto/room.py 等 REST DTO 一样使用 CamelModel：JSON 层 camelCase，Python 层
snake_case。

信封（ClientEnvelope/ServerEnvelope，定义在文件最后）故意把 `payload` 留成
`dict[str, Any]`，不是每个事件各自一份"信封+具体 payload 类型"的判别联合：
`type` 是运行时才知道的字符串，payload 的具体形状要看 type 是什么，pydantic
的模型定义没法在"这个字段的类型"层面表达"取决于另一个字段的值"这种关系
（除非再手动叠一层 discriminated union，对只有 6～20 个事件的量级来说是过度
设计）。ws.py 里的用法是：先用 ClientEnvelope 解出 type/playerId/原始
payload dict，再按 type 分支，把 payload dict 交给下面对应的具体 payload
模型再校验一次——这样信封层和 payload 层各自都是真正在被使用的模型，而不是
为了"看起来完整"摆在这里的装饰品。信封模型不进 scripts/export_schema.py 的
导出清单：payload 是 `dict[str, Any]`，导出成 JSON Schema/TS 只会得到一个
`{[k: string]: unknown}`，对 SDK 没有实际价值——SDK 那边的信封类型
（ServerToClientEvent）继续手写，见 trpg-sdk/src/types.ts。
"""

from typing import Any, Literal

from pydantic import Field

from app.dto.common import CamelModel, UtcDatetime
from app.dto.room import RoomPlayerRead

# ── 客户端 → 服务端 ──────────────────────────────


class RoomJoinPayload(CamelModel):
    """room.join 事件 payload。

    `reconnect_token` 必填：它是玩家在这个房间里的身份密钥（`players.reconnect_token`，
    建房/加入时下发给本人）。WS 连接握手只校验了「你是某个登录账号」，但连接
    时带的 playerId 是任意的、而且被公开房间预览暴露——只认 playerId 会让任何
    登录用户绑定成别人（冒充房主 game.start / 提交行动，PR #78 review 指出）。
    绑定时要求出示该玩家的 reconnect_token，才能证明「你就是这个玩家本人」。

    roomCode/nickname 是前端沿用原型习惯发送的冗余字段，服务端不读，保留可选
    以免影响现有调用方。
    """

    reconnect_token: str = Field(..., min_length=1)
    room_code: str | None = None
    nickname: str | None = None


class PlayerReadyPayload(CamelModel):
    """player.ready 事件 payload。

    `ready` 必填、不给默认值：协议上「设置准备状态」这个动作必须说清楚要设成
    什么，缺字段是一条畸形消息，应该被丢弃，而不是被悄悄当成 `False` 处理。
    这里给默认值的代价不只在后端——它会顺着 codegen 变成 SDK 的
    `ready?: boolean`，让 `setReady(playerId, {})` 也能通过类型检查并静默地把
    玩家设成未准备（见 PR #76 review）。改动前的手写 SDK 类型本来就是必填的。
    """

    ready: bool


class GameStartPayload(CamelModel):
    """game.start 事件 payload——目前不带任何字段。

    定义一个空模型（而不是完全跳过校验）是为了让 game.start 也走跟其它事件
    一致的"接收端过一次模型校验"路径，行为对齐、不搞特例。
    """


class ActionSubmitPayload(CamelModel):
    """action.submit 事件 payload——issue #107 定稿后玩家对 AI 主持人说的
    **唯一**事件（行动或提问都走它，"是哪种"由 AI 判断，协议层不预分类）。

    `utterance` 必填，理由同 PlayerReadyPayload.ready：一条不带行动内容的
    action.submit 是畸形消息。给默认空串会让 SDK 侧变成 `utterance?: string`，
    于是 `submitAction(playerId, {})` 类型检查通过、运行时静默无操作。

    注意「必填」只管字段存在，空白内容（`""` / `"   "`）仍由下游的
    `strip()` + 空值判断拦掉，两者不冲突。

    `summarized_from` / `visibility` 是 issue #107 铺的协议位：
    - `summarized_from`：这次提交是从讨论区哪几条消息总结来的（消息 id 列表）。
      本期只透传不消费——AI 编排（#48/#68）接手后决定怎么用。
    - `visibility`：`"private"` 表示「我偷偷摸他口袋」这类不想让其他玩家看到
      结果的私密行动。本期传 `private` 会收到 NOT_IMPLEMENTED 的 error——
      真正的私密裁决需要 AI 知道「结果只给发起者且后续叙事不能泄露」，属于
      编排层的活，硬做会做出一个会漏信息的假私密，比不做更糟（issue #107
      关键决策）。**不静默当成 public 处理**：把玩家以为保密的行动广播出去，
      当场就暴露了。
    """

    utterance: str = Field(max_length=2000)
    summarized_from: list[str] | None = None
    visibility: Literal["public", "private"] | None = None


class CheckRollPayload(CamelModel):
    """check.roll 事件 payload（issue #77 新增，feat/keeper-agent 两段式玩家
    掷骰实现）——玩家确认并结算一次守秘人已发起的待掷检定。

    `check_request_id` 必填：标识具体是哪一次待掷检定（守秘人裁决"需要
    检定"后随叙事一起广播的 `check.request` 事件带的那个 id）。骰值由服务端
    权威生成——这条消息本身不带任何"掷什么/掷多少"的信息，纯粹是"我确认
    掷这一个"。
    """

    check_request_id: str = Field(..., min_length=1)


class SanCheckRollPayload(CamelModel):
    """san.check.roll 事件 payload（issue #77 新增，feat/keeper-agent 两段式
    玩家掷骰实现）——同 CheckRollPayload，理智检定版本。"""

    check_request_id: str = Field(..., min_length=1)


class ChatSendPayload(CamelModel):
    """chat.send 事件 payload（issue #107）——玩家往**讨论区**发一条消息。

    讨论区跟「对 AI 主持人说话」（action.submit）是两条完全独立的通道：
    讨论区消息只在玩家之间广播，**永远不进任何 LLM 上下文**（成本 + 玩家
    需要"AI 听不见"的商量空间，这是 #107 的立项理由）。

    `client_message_id` 是客户端生成的去重键：断线重连后客户端可能重发同一条
    消息，服务端靠 `(player_id, client_message_id)` 唯一约束保证只落一行、
    重发拿到与第一次一致的广播。
    """

    text: str = Field(..., min_length=1, max_length=2000)
    client_message_id: str = Field(..., min_length=1, max_length=64)


# ── 服务端 → 客户端 ──────────────────────────────


class SessionBoundPayload(CamelModel):
    """session.bound 推送 payload。"""

    room_id: str
    player_id: str


class PartyUpdatePayload(CamelModel):
    """`party.update` 推送：这个玩家自己的空间处境（`exec/33 §5.4`）。

    🔴 **逐人裁过再发**，不是把全房间的分组表广播出去：别处那一组在哪、有谁，
    对你的角色而言是不该知道的（他们可能还在潜行）。所以这里只有
    「我在哪 · 谁跟我在一处 · 另有几组人在别处」——**够玩家看出系统把他放错了
    地方，又不泄露内容**。

    它存在的理由：真人实测里系统把队友拖进了地下室，而**界面上一处都没有位置
    信息**，于是没有任何人会发现。装上这只眼睛之后，静默错误变成可见错误。
    """

    #: 🔴 一个默认值都不给：服务端每次都送得出这五个字段，**契约就该说它一定在**。
    #: 给了默认值，生成的 TS 就是可选的，前端只能写 `?? 0` —— 那正是明令禁止的
    #: 静默兜底。可空的三个是**真的可能没有**（位置未记录 / 没有待确认），
    #: 它们是"必填但可为 null"，不是"可以不发"。
    location_id: str | None
    location_name: str | None
    #: 跟我在一处的人（含我自己）。
    companions: list[str]
    #: 另外有几组调查员在别处（只给数字，不给位置与名字）。
    other_groups: int
    #: 我这一轮走到了别人所在的地方，等我确认是不是真的碰上了（`exec/33 §5.2`）。
    merge_pending_at: str | None


class KeeperPhasePayload(CamelModel):
    """`keeper.phase` 推送：这一局走到哪一步了（2026-08-15）。

    🔴 **补的是一条只有一半的链**：`closure` 早就会把 phase 写成 `ending` /
    `finished`，叙事纪律与字数上限也照着它变，**但前端一个字都收不到**。
    玩家侧的表现是：说完「结束了吧」，收到一段普通叙事，界面毫无变化——
    「整条链都在，就是没人能用到」的又一处。

    ⚠️ 别跟 `RoomPhasePayload` 搞混：那个是**大厅级**的房间状态
    （Lobby/InGame/Completed），而且从来没有地方发出过。这一条是**对局内**
    的守秘人阶段（opening/investigation/ending/finished），两者粒度不同。

    只发 phase 与 ending_id，不发别的——收尾门里的数字（还剩几条配对没揭开）
    是守秘人的判断依据，给玩家看等于剧透进度条。
    """

    phase: str
    #: 命中了剧本预设结局时才有；开放式模组自然收尾时是 None。
    ending_id: str | None


class RoomPausePayload(CamelModel):
    """`room.pause` 客户端事件：暂停 / 恢复（`exec/35`）。

    一个事件带 bool，而不是 pause/resume 两个事件——「暂停中」是个状态位，
    两个事件会让"连点两次暂停"和"没暂停就恢复"各自需要一条规则。
    """

    paused: bool


class RoomPausedPayload(CamelModel):
    """`room.paused` 推送：房间暂停状态变了，附带是谁按的。"""

    paused: bool
    by_nickname: str


class TurnClarifyPayload(CamelModel):
    """`turn.clarify` 客户端事件：「你把我的话理解错了」（`exec/35`）。

    `clarification` 必填，理由同 `ActionSubmitPayload.utterance`：不带内容的
    纠错是畸形消息，而给默认空串会让 SDK 侧变成可选、于是静默无操作。

    🔴 **没有"纠正哪一轮"这个参数**：只能纠最新的一轮。翻旧账要能定位到
    任意一轮的世界状态，那是一整套 undo 基础设施；而且真人桌上纠错本来就
    只发生在刚刚那一拍。
    """

    clarification: str = Field(..., min_length=1, max_length=500)


class PartyMergeConfirmPayload(CamelModel):
    """`party.merge.confirm` 客户端事件：当事人确认「我确实跟他们碰上了」。

    没有对应的"否认"动作——不确认就是维持分离，那本来就是默认与安全方向。
    """


class LuckOfferPayload(CamelModel):
    """`luck.offer` 推送：骰子已经停下，问他要不要花幸运把失败推成成功
    （`exec/26 #66`）。

    **卡片本身就是教学位**——新手根本不知道有这条规则，只有主持人知道。所以
    差几点、花多少、剩多少全都写出来，而不是只给一个「消耗幸运」按钮。
    """

    #: 原样带回（`luck.decide` 的 `decisionId`）。
    decision_id: str
    player_id: str
    #: 掷的是什么技能——玩家要认得出这是刚才那一次。
    skill: str
    rolled: int
    target: int
    #: 花多少点（= 出目 − 成功率，线性无折扣，规则书明文）。
    cost: int
    #: 现在有多少点。花完剩 `luck_remaining - cost`。
    luck_remaining: int
    #: 对抗检定：**花了也可能还是输**（胜负要重算）。前端据此多说一句，
    #: 否则玩家花掉十几点却没赢，只会认为是 bug。
    opposed_opponent: str | None = None


class LuckDecidePayload(CamelModel):
    """`luck.decide` 客户端事件：花，或者不花。

    🔴 跟会合确认不同，这里**有"不花"这个动作**：会合不点就是维持分离（安全
    方向就是默认），而这里不答一句，那次检定的结果就一直悬着——整轮停在那儿。
    """

    decision_id: str = Field(..., min_length=1)
    accepted: bool


class KeeperBusyPayload(CamelModel):
    """`keeper.busy` 推送：守秘人正在别处忙（`exec/33 §5.4`）。

    分头时叙事是逐组生成的，没轮到的那一组屏幕上此前**什么都没有**，静默十几秒
    然后突然弹出一段。线下你至少看得见 KP 在跟别人说话。
    """

    busy: bool


class NarrationPushPayload(CamelModel):
    """narration.push 推送 payload。"""

    text: str
    # 只给你一个人的私密结果（exec/18 ⑥ 私密行动 / ② 潜行）。前端据此把这条
    # 折叠成「点按查看」——**线下同桌**时旁人看得见你的屏幕，私密的物理前提
    # 本来就不成立，这个交互是那种场合下唯一的补救（P5.3）。
    private: bool = False
    # 这条推送对应的 `events` 行 id。前端**按它去重**——replay 补历史与实时
    # 广播是两条路径，同一条叙事会两边各来一次。真人实测 2026-07-31（exec/19
    # #42）之前前端只能拿正文文本当身份，于是"同一句话说第二次"被永久吞掉。
    # 🔴 不要用自由文本当标识符（项目 CLAUDE.md 已有这条判据）。
    event_id: str | None = None


class NarrationDeltaPayload(CamelModel):
    """narration.delta 推送 payload（`exec/28`）——叙事流式到达的一段。

    🔴 **它不是新的事实来源。** `events` 表仍然只落一行完整叙事，
    `GET /rooms/{roomId}/replay` 一行不用改；delta 纯粹是实时通道的加速。
    重连的人拿 replay 的完整文本，**不重放流式**——刷新页面后把整局叙事重打
    一遍，玩家会疯（`exec/26 #62` 第一条要求）。

    每段都已经过完纪律层与泄密守门才发出（`runtime/narration_stream`）：
    **推出去的字不可撤回**，所以守门必须在推之前，不能在之后。
    """

    #: 与随后那条 `narration.push` 的 `eventId` 相同——前端据此把碎片拼到同一
    #: 条消息上，而不是新增一条。
    event_id: str
    #: 这条流里的序号，从 0 开始。去重键是 `(eventId, seq)`。
    seq: int
    text: str
    private: bool = False


class ChatMessagePayload(CamelModel):
    """chat.message 推送 payload（issue #107）——讨论区消息的房间广播。

    带 `client_message_id` 回传是为了让发送方把广播和自己本地乐观插入的
    那条对上号（去重/替换本地占位），其他人直接按新消息渲染。
    `sent_at` 用 UtcDatetime：所有对外时间字段必须带时区后缀，否则客户端
    会把 UTC 当本地时间解析（UTC+8 上「4 分钟前」显示成「8 小时前」的真 bug）。
    """

    message_id: str
    player_id: str
    nickname: str
    text: str
    sent_at: UtcDatetime
    client_message_id: str


class ActionBroadcastPayload(CamelModel):
    """action.broadcast 推送 payload（issue #107）——玩家对 AI 主持人说的
    **原话**的房间广播。

    修的是三人联机实测出的"聊天记录像被隔离"bug：此前玩家原话只在发送方
    本地插入气泡，其他人只能从守秘人回复的转述里看到内容。现在 action.submit
    先广播这条（谁、说了什么），再广播 AI 的叙事回复（narration.push），
    所有人看到的时间线一致——就像牌桌上说话大家都听得见。
    """

    player_id: str
    nickname: str
    utterance: str
    # 同 NarrationPushPayload.event_id：前端按事件 id 去重，不再按原话文本。
    event_id: str | None = None


class RoomStatePayload(CamelModel):
    """room.state 推送 payload（issue #77 新增，替代 HTTP 轮询伪广播）。

    本期协议槽位已留好（信封类型/校验器/SDK 方法齐全），但 ws.py 里没有任何
    地方会真的发出这个事件——大厅玩家列表仍然是前端 `GET /rooms/{roomCode}`
    轮询获取（issue"三处原型取舍"表格，真正切换依赖前端改动，本期不动
    trpg-frontend）。
    """

    room_id: str
    phase: str
    players: list[RoomPlayerRead]


class PlayerJoinedPayload(CamelModel):
    """player.joined 推送 payload（issue #77 新增，同上，本期不会真的发出）。"""

    player: RoomPlayerRead


class TurnBeginPayload(CamelModel):
    """turn.begin 推送 payload（issue #77 新增，回合制约束，本期不会真的发出）。"""

    player_id: str


class GameEndedPayload(CamelModel):
    """game.ended 推送 payload（issue #77 新增，触发复盘，本期不会真的发出）。"""

    reason: str | None = None


class ViewPrivatePayload(CamelModel):
    """view.private 推送 payload（issue #77 新增，私密视角/不泄底的载体）。

    本期协议槽位已留好，但 `narration.push` 仍然是全房间广播（issue
    "三处原型取舍"表格），没有任何地方会真的发出这个事件——真正的信息
    不对称需要规则引擎知道"这条叙事该给谁看"，归 #48/#68。
    """

    player_id: str
    text: str


class CheckRequestPayload(CamelModel):
    """check.request 推送 payload（issue #77 新增；feat/keeper-agent 起在
    keeper 模式下真的会发出——守秘人裁决需要检定后，不立即掷骰，而是随叙事
    一起推这条通知，玩家在前端点击「掷骰」确认后才真正生成骰值）。

    `check_request_id` 是这次待掷检定的标识，玩家确认时原样带回
    （`check.roll`/`san.check.roll` 的 payload）。非 keeper 模式（Fallback/
    DeepSeekNarrator）不会发出这个事件。
    """

    player_id: str
    skill: str
    target_value: int | None = None
    check_request_id: str
    reason: str | None = None


class CheckResultPayload(CamelModel):
    """check.result 推送 payload（issue #77 新增；feat/keeper-agent 起真的
    会发出）。

    直接返回终值，不做两段式初步结果（issue 决策 4：幸运消耗机制推迟，
    协议一并简化）——这里的"两段式"指的是幸运消耗，不要和"两段式玩家掷骰"
    （裁决/掷骰分离）混淆。
    """

    player_id: str
    skill: str
    roll_value: int
    target_value: int | None = None
    result: str
    check_request_id: str | None = None
    # 对抗检定（exec/19 #38）。全为 None = 普通检定，前端渲染与此前一致。
    opposed_opponent: str | None = None
    opposed_roll_value: int | None = None
    opposed_target_value: int | None = None
    opposed_result: str | None = None
    opposed_won: bool | None = None
    # 🔴 三态结论（2026-08-15）："胜"/"负"/"僵持"。`opposed_won: bool` 装不下
    # 第三种——实测里双方都失败被写成「负」，前端卡片也只能显示"负"，那是在
    # 说谎（COC 里双方都失败是维持现状，没有人得手）。
    # None = 老客户端/普通检定，前端回落到 `opposed_won`，渲染与此前一致。
    opposed_verdict: str | None = None
    # 幸运补正之后的有效出目与花掉的点数（2026-08-14 实测）。只改 `result` 而
    # 把 `roll_value` 原样留着，玩家看到的是「掷出 7、目标 5、成功」——说不通。
    # 两个都为 None = 这次没花幸运，前端渲染与此前一致。
    effective_roll_value: int | None = None
    luck_spent: int | None = None


class SanCheckRequestPayload(CamelModel):
    """san.check.request 推送 payload（issue #77 新增；feat/keeper-agent 起
    真的会发出，同 CheckRequestPayload 的理智检定版本）。"""

    player_id: str
    current_san: int | None = None
    check_request_id: str
    reason: str | None = None


class SanCheckResultPayload(CamelModel):
    """san.check.result 推送 payload（issue #77 新增，同 CheckResultPayload
    直接返回终值；feat/keeper-agent 起真的会发出）。"""

    player_id: str
    roll_value: int
    san_loss: int
    result: str
    check_request_id: str | None = None
    san_remaining: int | None = None


class CharacterStatChangedPayload(CamelModel):
    """character.stat_changed 推送 payload（feat/keeper-agent，真人实测 09-#4
    修复）——HP 变更的结构化广播。

    San 已经有 `san.check.result` 携带 `san_remaining`（走"检定→掷骰→广播
    结果"这条路），不需要这个事件；HP 变化是裁决直接判定伤害后立即执行，
    没有对应的检定/掷骰事件可以携带新值，此前只把结果拼进叙事正文当纯文本，
    前端角色卡拿不到任何结构化数据、HP 从进房间起就是建卡快照、永不更新。
    """

    player_id: str
    hp: int
    hp_max: int | None = None
    reason: str | None = None


class ClueGrantedPayload(CamelModel):
    """clue.granted 推送 payload（issue #77 新增，线索发现，本期不会真的发出）。"""

    player_id: str
    clue_name: str
    description: str | None = None


class ErrorPayload(CamelModel):
    """error 推送 payload（issue #77 新增）。

    发起者做不成的事要明说，不能静默丢弃（`continue`，见 ws.py 旧逻辑）让
    客户端干等。非 keeper 叙事实现下的 `check.roll`/`san.check.roll` 也走它
    回 NOT_IMPLEMENTED。
    """

    code: str
    message: str


# ── 信封 ────────────────────────────────────────


class ClientEnvelope(CamelModel):
    """客户端 → 服务端信封：`{type, playerId, payload}`。

    `payload` 留成未细分的 dict——具体形状要看 `type`，ws.py 拿到这层校验过
    的信封后，再按 `type` 把 `payload` 交给上面对应的具体 payload 模型校验。
    """

    type: str
    player_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ServerEnvelope(CamelModel):
    """服务端 → 客户端信封：`{type, payload}`。"""

    type: str
    payload: dict[str, Any]
