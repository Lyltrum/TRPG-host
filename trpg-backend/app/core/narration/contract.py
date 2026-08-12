"""叙事层的抽象契约（`exec/27` 阶段 1）。

🔴 **这个模块是叶子：它不 import 任何 `app.*` 的东西，永远不许 import。**

原先 `core/narrator.py` 同时扮演三个角色——抽象层、工厂、以及一个实现
（`RoomAwareKeeperNarrator`）。于是抽象反过来依赖了具体（`build_narrator` 要
构造 `KeeperAgent`），形成 5 组循环依赖，靠三处**函数内 import** 硬撑着。
那是绕过循环，不是消除。

拆开之后：任何人想加一个新的主持人实现，只需要对着本文件写，然后在
`factory.py` 里注册，**全程不碰 `keeper/` 一行**。这就是可插拔的实际含义。
"""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class PlayerUtterance:
    """本轮某一个玩家说的那句话（收集窗口合并前的原始条目）。

    keeper 需要**逐条**而不只是合并后的那一段：分头时门厅那段的上下文里
    不能出现地下室那位说了什么（exec/14 P5.2d）。
    """

    player_id: str
    nickname: str
    text: str


#: 叙事流式到达的一段（`exec/28`）。参数是 `(seq, text)`。
#:
#: 🔴 传进来的每一段**都已经过完纪律层与泄密守门**——推出去的字收不回来，
#: 所以守门在推之前。实现方不要再对它做任何裁剪。
#:
#: 不传就是不流式：`Narrator` 的实现可以完全忽略它（非 keeper 的 Fallback /
#: DeepSeekNarrator 就没有"边写边推"这个概念）。
NarrationDeltaSink = Callable[[int, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class NarrationContext:
    """生成一段叙事所需的全部上下文。调用方（WS 层）负责准备好这些字段——
    本模块不查库，也不知道房间/玩家在数据库里长什么样。

    （`NarrationDeltaSink` 定义在本类之前，因为下面的字段注解要用它。）"""

    utterance: str
    player_nickname: str
    module_title: str | None = None
    # 每条已格式化成"昵称: 内容"形状，由调用方从 `events` 表整理出来。
    recent_actions: list[str] = field(default_factory=list)
    # keeper agent（feat/keeper-agent 实验）需要的定位信息：它的工具要知道
    # 在哪个房间、为谁掷骰/改状态。单轮叙事实现（DeepSeek/Fallback）不读。
    room_id: str | None = None
    player_id: str | None = None
    # 本轮收集窗口里**一起发言**的全部玩家 id（含发起者）。空 = 只有发起者。
    # keeper 用它决定"这一轮把谁挪到新场景"——没发言的人位置不动（P5.2）。
    participant_ids: tuple[str, ...] = ()
    # 本轮**自己勾了私密**的玩家（exec/18 ⑥ `visibility="private"`）。他们的
    # 行动结果只回给本人，同处一地的其他人不知道。⚠️ 守秘人永远看得见——
    # 私密是玩家↔玩家，不是玩家↔KP。
    private_player_ids: tuple[str, ...] = ()
    # 本轮各人的原话，逐条。空 = 只有 `utterance` 这一句（心跳/开场/掷骰结算
    # 路径）。分组叙事时按受众裁剪，见 KeeperAgent._narrate_per_audience。
    utterances: tuple[PlayerUtterance, ...] = ()
    # 玩家纠错（`exec/35`）：非空 = 这一轮是**重裁上一轮**，`utterance` /
    # `utterances` 是上一轮的原话，这里是玩家补的那句「你理解错了：……」。
    #
    # 🔴 它是**代码判的**（玩家点了按钮），不是模型分类出来的——「这句话是不是
    # 在纠错」交给模型判就又多一层概率，而纠错本身正是用来兜模型判错的。
    clarification: str | None = None
    # 世界心跳主动轮（路线 6）：裁决/叙事走克制模式，不发起检定。
    is_heartbeat: bool = False
    # 开场仪式轮（设计 05）：game.start 后自动跑的第一轮，不发起高风险检定。
    is_opening_ceremony: bool = False
    # 聚光灯（exec/14 P5.2）：这一轮要把镜头转向谁。由导演层按「谁最久没被
    # 点到」算出来，非空时 keeper 强制注入引导。None = 普通心跳。
    spotlight_nickname: str | None = None
    # 叙事流式（exec/28）：传了就边写边推，不传就照旧攒完整段再发。
    # 🔴 它**不改变返回值**——`NarrationOutcome.text` 仍然是完整的一段话，
    # 落库、replay、历史全部照原样走。delta 只是提前把同样的内容送到玩家眼前。
    on_delta: NarrationDeltaSink | None = None
    # 分头叙事的流式口（`exec/33 §3.2`）。跟 `on_delta` 分开是因为分头时**没有
    # 「全房间那一段」**：每段各有各的受众与事件 id，所以这里要的是一个工厂，
    # 不是一个 sink。不传 = 分头段落照旧攒完整段再发（退化保证）。
    segment_delta_sink: "SegmentDeltaSinkFactory | None" = None


@dataclass(frozen=True, slots=True)
class CheckRequestNotice:
    """一次"待掷检定"的通知（两段式玩家掷骰）——守秘人裁决需要检定后不立即
    掷骰，而是把这条通知随叙事一起广播，玩家在前端点击确认后才真正掷骰。"""

    check_request_id: str
    kind: str  # "skill" | "san"
    player_id: str
    player_nickname: str
    skill: str | None  # kind="san" 时为 None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CheckResultNotice:
    """一次检定的服务端权威结果（玩家点击掷骰确认后产生）。"""

    check_request_id: str
    kind: str  # "skill" | "san"
    player_id: str
    skill: str | None  # kind="san" 时为 None
    rolled: int
    target: int
    level: str  # skill：成功等级文本；san："成功"/"失败"
    san_loss: int | None = None
    san_remaining: int | None = None
    # 对抗检定（exec/19 #38）：对手侧也由服务端掷骰，结果一起广播——玩家要
    # 看得见自己输在哪一掷，不能只给一个"你失败了"。
    opposed_opponent: str | None = None
    opposed_rolled: int | None = None
    opposed_target: int | None = None
    opposed_level: str | None = None
    opposed_won: bool | None = None


@dataclass(frozen=True, slots=True)
class StatChangeNotice:
    """一次 HP 修改的结构化通知（真人实测 09-#4：HP 变化此前只被拼进叙事
    正文当纯文本广播，前端角色卡拿不到任何结构化数据，HP 从进房间起就是
    建卡快照，永不更新）。San 已经有 `san.check.result` 事件携带
    `san_remaining`，不需要这个——San 走"检定→掷骰→广播结果"这条路，HP 是
    裁决直接判定伤害后立即执行，没有对应的检定/掷骰事件可以携带新值。"""

    player_id: str
    hp: int
    hp_max: int | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class NarrationSegment:
    """一段**只发给特定几个人**的叙事（exec/14 P5.2 分头探索）。

    `audience` 是这段该送达的玩家 id。**空元组 = 谁都不发**，不是"发给所有
    人"——受众算错时必须表现为没人收到（可见的故障），而不是当场泄密。
    """

    text: str
    audience: tuple[str, ...]
    #: 这段发生在哪个剧本节点（审计/落库用，None = 位置未记录）
    node_id: str | None = None
    #: 这段是「只给你一个人」的隐秘结果（②潜行 / ⑥私密行动），不是"这处的
    #: 大家都看得见"。前端据此折叠成点按查看——线下同桌旁人看得见你的屏幕。
    covert: bool = False
    #: 这段的事件 id。**流式时必须在开流之前就有**——delta 靠它跟最终那条
    #: `narration.push` 认亲（`exec/28`），所以身份要先于第一个字存在。
    #: 非流式路径为 None，由投递层落库时分配（行为与 `exec/33 §3.2` 之前一致）。
    event_id: str | None = None


#: 给「一段」取一个 delta 投递口：入参是这一段的事件 id 与受众，返回只发给
#: 这几个人的 sink（`exec/33 §3.2`）。
#:
#: 🔴 **受众是入参，不是 sink 自己去查**：并行叙事落地那一刻，全房间广播的
#: delta 就是泄露——两组同时在写，谁的字都会推到对面屏幕上。把受众放进签名里，
#: 是让"忘了裁"变成一个类型错误而不是一次静默的泄密。
SegmentDeltaSinkFactory = Callable[[str, tuple[str, ...]], "NarrationDeltaSink"]


class PlayerOffer(Protocol):
    """「等某个玩家答一句」的待决定项——**契约层只需要知道这几件事**。

    🔴 写成 Protocol 而不是 import `PendingDecision`：本模块是叶子，
    `keeper.runtime.pending` 反过来要 import 它。**`TYPE_CHECKING` 下的 import
    也算依赖边**——架构测试是 AST 扫的，它当场抓到了这条环（这正是那条判据说的
    「架构约束必须有测试守护」）。结构化子类型让运行时那个 dataclass 自动满足它，
    两边都不用认识对方。

    kind 专属的数据（幸运卡的花费/余额）在 `payload` 里，投递层自己解。
    """

    @property
    def decision_id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def player_id(self) -> str: ...

    @property
    def player_nickname(self) -> str: ...

    @property
    def payload(self) -> dict[str, Any]: ...


#: 「骰子已经掷出来了」的即时回调（见 `Narrator.resolve_check`）。
CheckResultCallback = Callable[["CheckResultNotice"], Awaitable[None]]


@dataclass
class NarrationOutcome:
    """`Narrator.narrate()`/`resolve_check()` 的统一返回形状。

    `text` 是要广播的叙事（两段式掷骰的"重发请求"分支可能为空串——彼时不该
    广播一条空 narration.push）；`check_requests`/`check_results` 是本轮新
    发起/新结算的检定通知，调用方（WS 层）负责把它们各自广播成
    `check.request`/`check.result` 事件。`stat_changes` 是本轮发生的 HP
    变更（`character.stat_changed` 事件）。非 keeper 的单轮叙事实现三个列表
    恒为空。"""

    text: str
    check_requests: list[CheckRequestNotice] = field(default_factory=list)
    check_results: list[CheckResultNotice] = field(default_factory=list)
    stat_changes: list[StatChangeNotice] = field(default_factory=list)
    #: 分头探索（P5.2）：各处各看各的。**非空时 `text` 必为空**——同一轮不会
    #: 既有全房间叙事又有分组叙事，否则两边内容会重复。未分头时它恒为空，
    #: 调用方走的还是原来那条 `text` 广播路径。
    segments: list[NarrationSegment] = field(default_factory=list)
    #: 「结算之后还要等某个玩家答一句」的待决定项（`exec/34` 第 4 步，现在只有
    #: 幸运消费）。调用方（WS 层）负责渲染成卡片。
    #:
    #: 形状见 `PlayerOffer`——契约层不认识 `PendingDecision`（它是叶子）。
    player_offers: list[PlayerOffer] = field(default_factory=list)


class Narrator(ABC):
    """叙事生成器接口。"""

    @abstractmethod
    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        """根据上下文生成一段叙事文本（及本轮的检定请求/结果通知）。"""

    async def resolve_check(
        self,
        room_id: str,
        player_id: str,
        check_request_id: str,
        on_result: CheckResultCallback | None = None,
    ) -> NarrationOutcome:
        """结算一次玩家确认的掷骰（两段式玩家掷骰）。

        `on_result`：**骰子一落地就回调**，不等结算叙事。骰值本身是纯代码、
        毫秒级，而它后面那次结算叙事是 10 秒级的 LLM 往返——两件事一起等完再
        广播，玩家点完「投掷」要盯着屏幕十几秒才看得到自己掷了多少（真人实测
        反馈）。传了这个回调，WS 层就能先把 `check.result` 推出去。

        ⚠️ 结果**仍然会**出现在返回的 `check_results` 里（老调用方不受影响）。
        用了回调的调用方要自己去重，别广播两遍。

        默认不支持：单轮叙事实现（Fallback/DeepSeek）没有"待掷检定"的概念，
        WS 层收到 check.roll/san.check.roll 时应把 NotImplementedError 转成
        NOT_IMPLEMENTED 错误事件，而不是让它把整条连接炸掉。
        """
        raise NotImplementedError

    async def resolve_player_offer(
        self,
        room_id: str,
        player_id: str,
        decision_id: str,
        accepted: bool,
        on_result: CheckResultCallback | None = None,
    ) -> NarrationOutcome:
        """玩家答完「结算之后那一拍」（`exec/34` 第 4 步，现在只有幸运消费）。

        骰子已经停下、结果还没生效的那个窗口里问出去的问题，答完才继续走生效
        与结算叙事。`accepted=False` = 不花，原样放行。

        默认不支持，理由同 `resolve_check`。
        """
        raise NotImplementedError
