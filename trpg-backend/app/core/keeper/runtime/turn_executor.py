"""回合编排（L4）：`KeeperDecision` 字段 → tools.py 对应 `*_impl` 的调度表。

跟 `decision.py`（L1 契约：裁决长什么样）分开——这里是"LLM 声明了哪个
字段就调哪个执行函数"的编排逻辑，不是数据定义。裁决产出分两条路径执行：

- `execute_side_effects`：HP 变更 + 状态记账，纯代码立即执行（LLM 摸不到
  骰子也改不了账）；
- `create_pending_checks`：checks/san_checks **不再在这里掷骰**——两段式
  玩家掷骰下，骰子由玩家在前端点击确认后才服务端权威生成，这里只把裁决
  产出的检定请求解析成待掷记录（`pending.PendingDecision`），真正的掷骰在
  `KeeperAgent.resolve_check` 里发生（见 agent.py）。
"""

import structlog

from app.core.keeper.access.subject import KEEPER, Subject, authorize_decision, sanitize_decision
from app.core.keeper.capabilities import executors, pendings
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.registry import PendingContext, TurnFacts
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.pending import PendingDecision

logger = structlog.get_logger()


async def execute_side_effects(
    deps: KeeperDeps, decision: KeeperDecision, *, subject: Subject = KEEPER
) -> tuple[list[str], list[str]]:
    """执行裁决里"立即生效"的部分：HP 变更 + 状态记账 + 议程触发。返回 (执行报告, 问题清单)。

    检定/理智检定不在这里执行——两段式玩家掷骰下骰子由玩家确认后才生成，
    见 `create_pending_checks`。

    - 执行报告：每项 `*_impl` 的完整返回文本，喂给叙事阶段——叙事者必须知道
      "发生了什么"才能如实写；
    - 问题清单：裁决里不合法的项（找不到的玩家 / 未知议程 id）**跳过不炸**，
      记下来一并喂给叙事阶段让它自然圆场；同时进日志供排查。

    执行是顺序的（不并发），tools 层的 write_lock 因此在这条路径上只是冗余
    保险——保留它是因为 `*_impl` 还可能被未来的并发调用方复用。
    """
    report: list[str] = []
    issues: list[str] = []

    # 执行边界的授权（exec/14 P2 纵深防御第二道）。受限主体的越权字段本来
    # 就无法表达（build_decision_model 已经把它从 schema 里去掉），这里再查
    # 一遍是因为：老 schema 反序列化出来的决策、测试构造的决策、以后从别处
    # 传进来的决策都可能绕过第一道。守秘人持全权限，这段对它恒为空。
    issues.extend(authorize_decision(subject, decision))
    decision = sanitize_decision(subject, decision)

    # 🔴 **本函数已经完全由注册表驱动**（exec/27 阶段 3 收尾）：这里不再有任何
    # 一片能力的名字，加一片能力不改这里一行。
    #
    # 顺序由各能力注册时的显式 `order` 决定，不能靠字典序或 import 顺序——它有
    # 语义：`moves` 必须排在 `current_node_id` 之后（否则逐人位置会被"本轮发言者
    # 的默认落点"盖回去），而执行报告的行序会原样喂给叙事阶段，顺序变了叙事读到
    # 的"发生了什么"就变了。
    # 本轮事实黑板：上游能力 publish、下游 consume，顺序由各自的 order 保证。
    # 这条通道存在的唯一理由是「当前场景」——地名归 world_state 的自由文本记账，
    # 而"场景变了却没给节点 id 就清空指针"是 movement 的规则（exec/19 #48）。
    # 切分之初 movement 直接读 world_state 的字段，那是隐式耦合；见 `TurnFacts`。
    facts = TurnFacts()
    for hook in executors():
        hook_report, hook_issues = await hook.run(deps, decision, facts)
        report.extend(hook_report)
        issues.extend(hook_issues)

    if issues:
        logger.warning("keeper_decision_issues", issues=issues)
    return report, issues


async def create_pending_checks(
    deps: KeeperDeps, decision: KeeperDecision, *, subject: Subject = KEEPER
) -> tuple[list[PendingDecision], list[str]]:
    """把裁决里的检定解析成待掷记录——**本函数不掷骰**。

    两段式玩家掷骰下，骰子由玩家在前端点确认后才由服务端权威生成（见
    `agent.resolve_check`）。这里只负责"哪些检定发得出去"。

    🔴 **完全由注册表驱动**（exec/27 阶段 3 收尾）：具体是技能检定还是理智检定，
    由各能力的 `pending` 钩子回答，这里不出现任何一片能力的名字。顺序由钩子的
    `order` 决定——待掷队列的顺序就是玩家看到掷骰卡片的顺序。

    返回 (待掷记录, 问题清单)。非法项跳过并记 issue，不炸整轮。
    """
    from app.models.room import Room

    pending: list[PendingDecision] = []
    issues: list[str] = []

    # 发起检定同样是受权限管辖的动作（exec/14 P2）。无 REQUEST_CHECK /
    # REQUEST_SAN_CHECK 的主体连 schema 里都没有这两个字段，这里是第二道。
    decision = sanitize_decision(subject, decision)

    # 🔴 护栏与账本都按**掷骰的那个人所在的场景**判定（P5.2）。分头探索后
    # 房间不再只有一个"当前场景"——用房间级指针去卡在地下室那位的检定，
    # 会拿书房的 checks[] 去否掉地下室的合法检定。所以钩子拿到的是整份
    # keeper_state，由它自己按人查位置。
    keeper_state: dict | None = None
    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room and isinstance(room.keeper_state, dict):
            keeper_state = room.keeper_state
    raw_scene = (keeper_state or {}).get("当前场景")
    current_scene = str(raw_scene) if raw_scene is not None else None

    async with deps.session_factory() as db:
        context = PendingContext(db=db, keeper_state=keeper_state, current_scene=current_scene)
        for hook in pendings():
            hook_pending, hook_issues = await hook.run(deps, decision, context)
            pending.extend(hook_pending)
            issues.extend(hook_issues)

    if issues:
        logger.warning("keeper_pending_check_issues", issues=issues)
    return pending, issues
