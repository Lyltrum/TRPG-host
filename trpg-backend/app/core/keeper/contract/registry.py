"""能力注册表的**机制**（exec/27 阶段 2）——本文件不认识任何一个具体能力。

## 它在解决什么

一个守秘人能力（"HP 变化"、"技能检定"、"分头移动"）天然横跨好几层：schema
教模型能说什么、prompt 教模型什么时候说、executor 把它变成世界的改变、
situation 把结果再喂回模型眼前、audit 让它在日志里留得下痕。此前这几段分别
躺在 `decision.py` / `prompts.py` / `tools.py` / `agent.py` 里——实测一个功能
平均改 4.1 个文件，`agent.py` 被 90 个功能里的 46 个碰过。**那不是文件放错了
地方，是能力被按技术层切碎了。**

本模块定义那些钩子的形状。具体能力在 `capabilities/<名字>/` 里各自组装一个
`KeeperCapability` 交上来，骨架（decision/prompts/turn_executor/agent）只跟
这份契约打交道。

## 🔴 为什么这里是叶子

`capabilities/*` 要 import 它（取 `Capability` 枚举、`DecisionModel` 基类），
所以它**一个 `app.*` 都不能 import**——否则 `contract → capabilities → contract`
当场成环。`ScenarioModule`/`KeeperDeps` 只在 `TYPE_CHECKING` 下引入，运行时
不产生依赖边。阶段 0 的架构测试盯着这条。

## 顺序为什么必须显式

`order` 一律是显式数字，不靠字典序、不靠 import 顺序（`exec/27` 障碍 1/2）：

- schema 片段合并后字段顺序要稳定，否则同一份裁决 schema 会抖；
- 裁决 prompt 的规则是**带编号的**（规则 1–12），块的先后有语义；
- 执行顺序有语义（`moves` 必须排在 `current_node_id` 之后，否则被默认值盖掉）；
- 局面块的先后决定模型先看到什么。

留出的间隔（10/20/30…）是给后续能力插队用的，插进去不必重排别人。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注，运行时不产生依赖边
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.keeper.contract.module_loader import ScenarioModule
    from app.core.keeper.runtime.deps import KeeperDeps
    from app.core.keeper.runtime.pending import PendingDecision
    from app.core.narration.contract import CheckResultNotice
    from app.dto.game import RulesetRead


class DecisionModel(BaseModel):
    """裁决 schema 的公共基类。

    裁决 JSON 由 LLM 生成，多给的字段忽略、不报错——校验的重点是"必要的结构
    在"，不是"一个字都不能多"。

    放在这里而不是 `decision.py`，是因为各能力的字段片段都要继承它，而
    `decision.py` 反过来要 import 那些片段。
    """

    model_config = ConfigDict(extra="ignore")


class Capability(StrEnum):
    """一个主体**有权做**的事。与 `KeeperDecision` 的动作字段一一对应。

    从 `subject.py` 挪到这里：能力片段（`capabilities/*/schema.py`）要声明
    "我这个字段需要哪种权限"，而 `subject.py` 是 `decision.py` 的下游，
    留在那儿会成环。
    """

    REQUEST_CHECK = "request_check"
    REQUEST_SAN_CHECK = "request_san_check"
    ADJUST_HP = "adjust_hp"
    #: 「他身上多了/少了一件东西」。与 ADJUST_HP 分开：血是规则算出来的数，
    #: 随身物品是**玩家自己写的清单**，改的是两种不同的东西。
    CHANGE_EQUIPMENT = "change_equipment"
    UPDATE_STATE = "update_state"
    SET_SCENE = "set_scene"
    #: 「此刻藏着没」。与 SET_SCENE 分开而不是共用一条：玩家向守秘人提问那一轮
    #: 要收走移动与场景指针，但**不该让藏起来的人现身**（exec/27 阶段 3 · B 族）。
    SET_HIDING = "set_hiding"
    #: 「他从临时性疯狂里缓过来了」。与 SET_HIDING 同族单列：疯狂是已经成立
    #: 的状态，不因为这一轮玩家只是问了句话就该被收走。
    CLEAR_MADNESS = "clear_madness"
    #: 「有件事还悬着 / 那件事了结了」。同上，也是已经成立的处境。
    TRACK_THREADS = "track_threads"
    FIRE_AGENDA = "fire_agenda"
    #: 揭开一条线索密级配对（原 REVEAL_VISIBILITY，exec/27 三处撞名一起改）。
    REVEAL_CLUE = "reveal_clue"
    ADVANCE_PHASE = "advance_phase"


#: 裁决 prompt 里的插槽。规则清单与输出格式示例是两处独立的有序文本，
#: 一个能力通常两处都要贡献一句。
PromptSlot = Literal["rules", "output_example"]


@dataclass(frozen=True)
class PromptBlock:
    """裁决 prompt 里的一段文本。

    `text` 是**成品原文**（含"3b."这样的编号），骨架只负责按 order 排好后
    拼起来——编号语义属于能力自己，骨架不该替它编号。
    """

    slot: PromptSlot
    order: float
    text: str


@dataclass(frozen=True)
class SituationContext:
    """渲染局面块要用到的全部输入。

    🔴 **它是被第四片能力撑出来的。** 前三片（health/agenda/progression）只需要
    「剧本 + keeper_state」，于是第一版签名就写成了那两个参数。切 `clue_reveal`
    时才发现它要按**观察者**渲染（哪条配对对**这个玩家**揭开了），两个参数怎么
    也表达不出来。

    与其给 render 不断加位置参数，不如给它一个可以长的上下文对象——加字段不必
    改已有能力的签名。同项目判据：**参数表是会长的，长在一个对象里比长在签名里
    便宜。**
    """

    module: ScenarioModule
    keeper_state: dict | None
    #: 这一块是渲染给谁看的（`None` = 守秘人自己的全量视图）。
    observer_id: str | None = None
    #: 在场调查员 `(player_id, 昵称)`。`movement` 的「各自所在」要按人渲染。
    #:
    #: 这是本对象**第二次**因为新一片能力而加字段——正好印证了当初不给 render
    #: 加位置参数的选择：加字段不必回去改任何已有能力的签名。
    players: tuple[tuple[str, str], ...] = ()
    #: 正挂着「你跟他们碰上了吗」的人（`exec/34`）。分组要用它，而它的真相在
    #: 待决定队列里、**要查库**——渲染钩子拿不到 db，所以由 `build_situation`
    #: 查好带进来。这是第三次因为新需求加字段，同一个理由。
    merge_pending: frozenset[str] = frozenset()
    #: 这一局用的规则数据。第四次加字段：`madness` 的局面块要把 symptom_id
    #: 翻成人看得懂的症状名，而那张表属于规则系统（`RulesetRead`），不属于
    #: 引擎——keeper_state 里存的只有 id。
    #:
    #: 可空：默认 None 让既有能力与既有测试的构造一个字都不用改。用得上它的
    #: 能力自己判断"没有规则数据"该怎么表现（`madness` 的表现是整块不渲染，
    #: 不是编一个症状名）。
    ruleset: RulesetRead | None = None


@dataclass(frozen=True)
class SituationBlock:
    """往「局面块」贡献一段模型每轮都看得见的状态。

    🔴 这个钩子是切 health 试点时**在真代码里发现漏掉的**（`agent.py` 那行
    `format_npc_states`）：能力不只要能改世界，还得让模型**看见**自己改成了
    什么样，否则下一轮裁决只能从上一段散文里猜（`exec/19 #39` 的原始症状）。

    `render` 返回空串 = 本轮没有内容，整块连标题一起不渲染。

    🔴 `keeper_only=True` = **这块只给裁决器看，不进叙事器的上下文**
    （`exec/23 #77`）。局面块两阶段共用，而有些块整段都是**写给裁决器的指令**
    ——「理智检定点」那块连"必须在 `san_checks` 里发起、数值照抄下面"都写在
    里面，于是叙事器也读到了 `0/1D6`，真机上直接念给了玩家听。
    **保密靠拿不到，不是请它别说**：这类块的正解是不喂，不是在叙事 prompt 里
    再加一条"别念机制"。默认 `False`（现有块行为逐字不变）。
    """

    order: float
    heading: str
    render: Callable[[SituationContext], str]
    keeper_only: bool = False


#: 审计钩子：从本轮裁决里挑出该进 `keeper_decision` 结构化日志的字段。
#:
#: 🔴 这是第五个钩子，跟 `situation` 一样是**切到一半在真代码里发现漏掉的**。
#: 没有它，`agent.py` 那行 `logger.info("keeper_decision", ...)` 就得逐个列出
#: 每个能力的字段——于是"加一个能力不改编排层一行"当场不成立，而且漏了不会
#: 报错，只是那片能力从此在日志里**隐身**：线上排查时看不出它本轮做没做事。
AuditFn = Callable[[BaseModel], Mapping[str, object]]


@dataclass(frozen=True)
class PendingContext:
    """两段式玩家掷骰里，"把裁决解析成待掷记录"这一步的共享输入。

    🔴 **第七个钩子的上下文。** `create_pending_checks` 是独立于
    `execute_side_effects` 的第二条执行路径（检定不当场掷骰，先排队等玩家点，
    见 `pending.py`），所以它需要自己的钩子——否则 `skill_check` / `san_check`
    两片根本切不出去。

    共享输入放这里而不是让每片各查一遍：`keeper_state` 与「当前场景」两片都要，
    数据库会话也只该开一次。
    """

    db: AsyncSession
    keeper_state: dict | None
    #: `state_updates` 里那个人类可读的场景名（护栏按它找剧本节点）。
    current_scene: str | None


#: 待掷钩子：把本轮裁决里属于自己的检定解析成待掷记录，返回 (待掷记录, 问题清单)。
#: **不掷骰**——骰子由玩家在前端点确认后才服务端权威生成。
PendingFn = Callable[
    ["KeeperDeps", BaseModel, PendingContext], Awaitable[tuple[list["PendingDecision"], list[str]]]
]


#: 结算钩子的前一半：把一条待掷记录变成一次真实的掷骰结果。
#: **服务端权威**——骰子由代码掷，模型只消费结果，改不了点数。
#: 🔴 **只读库、只掷骰**，一个字都不许写（见 `SettleHook`）。
SettleFn = Callable[["KeeperDeps", "PendingDecision"], Awaitable["CheckResultNotice"]]

#: 结算钩子的后一半：把这次结果真正落到世界上（写角色卡 / 记事件 / 给叙事的
#: 那句文本 / 解除隐匿……）。骨架在**广播结果之后**调它。
#:
#: 🔴 它的输入只有「待决定项 + 结果通知」，**两样都是能落库的**：幸运消费要
#: 等玩家隔着一次 WS 往返（乃至一次进程重启）才决定，那期间只有数据库活着。
#: 第一版把生效做成掷骰时捕获的闭包，形状上就跨不过这个等待。
ApplyFn = Callable[["KeeperDeps", "PendingDecision", "CheckResultNotice"], Awaitable[None]]


@dataclass(frozen=True)
class SettleHook:
    """`kind` 是 `PendingDecision.kind`：哪一片能力认领哪一种待掷记录。

    🔴 第八个钩子。此前"发起"已经注册表化（`pending` 钩子），而"结算"还是
    `agent.resolve_check` 里一条按 kind 写死的 if/else——**同一件事的两头，
    一头可插拔一头写死**。加一种新检定时前一半能自动接上、后一半会静默走进
    else 分支（当成 SAN 检定结算）。

    🔴 **掷骰（`run`）与生效（`apply`）是两步**（`exec/34` 第 3 步，起因是
    `exec/26 #66`）：幸运消费能把失败推成成功，而它发生在**广播结果之后**
    ——玩家看见骰子停下，才决定要不要花。若副作用留在掷骰那一步里，花完幸运
    就得**逐个回滚**（记账、解隐匿、写给叙事的文本……），那是打地鼠：下一个
    副作用照样漏，而且不会有任何东西变红（`#46` 加解隐匿时就没人回来更新
    `#66` 的时序图）。拆成两步之后副作用天然全落在决定之后，一个都不用回滚。

    两半写在同一行注册里，是因为它们正是那条判据说的「同一件事的两头」——
    分开注册就会有人只加一半。约束由 `tests/test_roll_before_apply.py` 守着。
    """

    kind: str
    run: SettleFn
    apply: ApplyFn


@dataclass(frozen=True)
class PendingHook:
    order: float
    run: PendingFn


#: 结算之后要不要再等玩家一拍：返回一项新的待决定项，或 None（不打扰）。
OfferFn = Callable[
    ["KeeperDeps", "PendingDecision", "CheckResultNotice"], Awaitable["PendingDecision | None"]
]

#: 玩家答完那一拍：返回 (它挂着的那条掷骰记录, 可能被改写过的结果通知)。
#: 改写是这个钩子存在的理由——花掉幸运会把「失败」推成「成功」，而后面每一步
#: （生效、事实账本、结算叙事）都必须看到改写后的那一份。
ResolveOfferFn = Callable[
    ["KeeperDeps", "PendingDecision", bool],
    Awaitable[tuple["PendingDecision", "CheckResultNotice"]],
]


@dataclass(frozen=True)
class PostSettleHook:
    """**第九个钩子**：结算之后还要再等玩家一拍（`exec/26 #66` 预言的那个）。

    🔴 前八个钩子覆盖两种形状——"裁决→执行"和"发起→结算"。幸运消费是第三种：
    骰子已经停下、结果还没生效，这中间要问玩家一句。`PendingFn` 的输入是**裁决**，
    从签名上就接不住它（它的输入是骰子结果）；`SettleHook` 方向相反（把待掷记录
    消费掉，不产出新的等待）。

    `kind` 是它产出的待决定项的 kind——`resolve` 按它反查回来。
    """

    kind: str
    order: float
    offer: OfferFn
    resolve: ResolveOfferFn


@dataclass
class TurnFacts:
    """能力之间按执行顺序传递的**本轮事实**。

    🔴 它解决的是一条真实存在的领域耦合：「当前场景」是人类可读地名，走
    `world_state` 的自由文本记账；而「场景变了却没给出剧本节点 id 就要清空
    节点指针」（`exec/19 #48`）是 `movement` 的规则。切分之前这条规则直接读
    `decision.state_updates`——**一片能力伸手进另一片的字段**。

    没有 import 跨过去，所以架构测试抓不到；但耦合是真的，而且是最坏的那种：
    **隐式的**。改 `world_state` 的人不会知道有人在读他的字段。

    正解不是加注释，是把它变成**显式契约**：上游能力 `publish`，下游能力
    `consume`，顺序由各自注册的 `order` 保证（`world_state`=20 早于
    `movement`=30，`test_capability_registry` 盯着这条）。字段而不是自由键的
    黑板——同项目判据：**不要用自由文本当标识符**。
    """

    #: 本轮裁决声明的新「当前场景」（人类可读地名）。`world_state` 写，
    #: `movement` 读。没声明就是 None。
    scene_name_declared: str | None = None

    #: 本轮**写了**「当前场景」但值跟上一轮**一模一样**。同样 `world_state`
    #: 写、`movement` 读。
    #:
    #: 🔴 它跟 `scene_name_declared` 是互斥的两半，缺了它就没法区分「没提场景」
    #: 和「明说了场景没变」——而这两件事对节点指针的含义完全不同：
    #: 前者是"这一轮我用 current_node_id 表达移动"（合法），后者是"我确认还在
    #: 原地"，那时再把指针挪到别的节点就是**自相矛盾**。
    #:
    #: 实据（2026-08-14 真人实测）：玩家在温特公寓连查四轮，每一轮裁决都写
    #: `当前场景=温特公寓`（原样重写）**同时**把 `current_node_id` 改成
    #: `investigation-start`。十次错误全是这个形状，一次例外都没有。
    scene_name_restated: bool = False

    #: 本轮真的揭开了新线索。`clue_reveal`（order=70）写，`closure`（order=85）读
    #: ——**还在往外掏线索的那一轮不许收尾**。
    #: 注意是"真的揭开了"而不是"裁决里写了"：编造的 pair id 会被 clue_reveal 跳过，
    #: 那种轮次不该算内容还在推进（同族于「写了 ≠ 变了」）。
    clues_revealed_this_turn: bool = False

    #: 本轮世界**真的往前走了一步**——只数有 id、代码点得清的那几样：
    #: 议程触发、既成事实落下、悬而未决开或结清。
    #: `open_threads`(55)/`established`(56)/`agenda`(60) 写，`closure`(85) 读。
    #:
    #: 🔴 存在理由（2026-08-18 真机）：「无进展轮数」原来的口径只认「去了新
    #: 节点 or 揭开新线索」，而那一局 19 拍里目睹了枪杀、拿到了主线线索、
    #: 触发了绑架议程、开合了 4 条悬而未决——**一样都不算进展**，那个数一路
    #: 涨到 15。它不是没有消费方的闲数：超过 `STALL_PUSH_THRESHOLD` 之后，
    #: 局面块里那一行会从「参考」升级成「本轮的硬要求：给推力」。于是整局都在
    #: 告诉模型"这桌人在原地打转"。
    #:
    #: **不含线索**：那一样由 `clues_revealed_this_turn` 单独表达，收尾门要
    #: 单独读它（"还在往外掏线索的那一轮不许收尾"），合并会把两个判据搅在一起。
    world_advanced_this_turn: bool = False

    #: 上面那个布尔是**被谁**置位的（能力名，按置位顺序）。
    #:
    #: 🔴 **只给探针用，不参与任何判断**（2026-08-18 双人真机）：那一局
    #: 5 拍打同一场僵局，「无进展轮数」只涨到 2——因为每拍都记下一条既成事实
    #: 就把它清零了。当时是**事后翻 `keeper_state` 猜出来的**，而日志里只有
    #: 一个 `world_advanced=True`，回答不了"是谁"。
    #:
    #: 加它不是为了改判据（判据这一轮不动），是为了让下一次改判据时**有证据**。
    world_advanced_by: list[str] = field(default_factory=list)

    #: 本轮为**哪些玩家 id** 发起了潜行检定。`skill_check`（order=5）写，
    #: `movement`（order=30）读。
    #:
    #: 🔴 存在理由：`hiding.hidden=true` 与 `checks:[stealth]` 是两条互不相干
    #: 的路，于是回归实测里出现了**藏起来是白给的**——潜行检定被护栏吞掉，
    #: 隐匿状态照样写进去，两次都是（第二次是贴到三步外的怪物旁边）。
    #: 有了这个事实，`movement` 才能把"进入隐匿"这一步让给检定结算去做。
    #:
    #: 装的是**玩家 id 不是昵称**：`hiding.player` 与 `checks[].player` 都是
    #: 模型写的自由文本，两边各自解析成 id 之后才谈得上比较——拿两个自由文本
    #: 直接比就是同义词打地鼠（exec/17）。
    stealth_check_players: set[str] = field(default_factory=set)


#: 执行钩子：拿到本轮裁决，做完自己那部分副作用，返回 (执行报告, 问题清单)。
#: 报告喂给叙事阶段（叙事必须知道"发生了什么"），问题清单是"裁决里不合法的项"
#: ——跳过不炸，一并交给叙事自然圆场。
ExecutorFn = Callable[["KeeperDeps", BaseModel, TurnFacts], Awaitable[tuple[list[str], list[str]]]]


@dataclass(frozen=True)
class ExecutorHook:
    order: float
    run: ExecutorFn


@dataclass(frozen=True)
class KeeperCapability:
    """一个能力交给系统的全部东西。

    `schema` 是这个能力贡献给 `KeeperDecision` 的字段片段（一个 mixin 模型）。
    ⚠️ 它**不会**被自动挂上去——`decision.py` 里那行显式继承才是权威，这里
    留一份是为了让 `tests/test_capability_registry.py` 能反过来验证"注册了
    却忘了继承"。动态拼基类可以省掉那一行，但会让静态类型检查彻底看不见
    `decision.hp_changes`，代价不划算。
    """

    name: str
    schema: type[BaseModel] | None = None
    #: 决策字段 → 动这个字段需要的权限（`subject.authorize_decision` 用）。
    field_capabilities: Mapping[str, Capability] = field(default_factory=dict)
    prompt_blocks: Sequence[PromptBlock] = ()
    executors: Sequence[ExecutorHook] = ()
    situations: Sequence[SituationBlock] = ()
    #: 往 `keeper_decision` 日志与 `keeper.decision` 事件贡献的字段
    #:（None = 这个能力没什么好审计的）。
    audit: AuditFn | None = None
    #: 两段式玩家掷骰·发起：把裁决解析成待掷记录（见 `PendingContext`）。
    pendings: Sequence[PendingHook] = ()
    #: 两段式玩家掷骰·结算：玩家点了之后怎么掷、怎么组装结果。
    settlers: Sequence[SettleHook] = ()
    #: 结算之后再等玩家一拍（第九个钩子，见 `PostSettleHook`）。
    post_settles: Sequence[PostSettleHook] = ()
    #: 这个能力在 `keeper_state` 里占的键。声明出来同时管两件事：
    #: **`state_updates` 不许写**，且**不原样喂给模型**（模型看到的是 situation
    #: 钩子渲染好的那一块）。
    #:
    #: 🔴 第六个钩子，切 `agenda` 时暴露的。原本 `tools._RESERVED_STATE_KEYS`
    #: 与 `agent._hidden_keys` 是**两张各自手维护的清单**，实测已经分叉：
    #: `NPC状态` 两张都没进，于是模型一条 `state_updates` 就能把 NPC 血量记录
    #: 覆盖成一个字符串、`load_npc_states` 静默返回 {}。所以这里只有一张清单，
    #: 「代码记账的键不原样喂给模型」由代码保证而不是靠两处同步。
    reserved_state_keys: Sequence[str] = ()
