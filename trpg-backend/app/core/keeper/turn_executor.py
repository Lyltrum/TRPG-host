"""回合编排（L4）：`KeeperDecision` 字段 → tools.py 对应 `*_impl` 的调度表。

跟 `decision.py`（L1 契约：裁决长什么样）分开——这里是"LLM 声明了哪个
字段就调哪个执行函数"的编排逻辑，不是数据定义。裁决产出分两条路径执行：

- `execute_side_effects`：HP 变更 + 状态记账，纯代码立即执行（LLM 摸不到
  骰子也改不了账）；
- `create_pending_checks`：checks/san_checks **不再在这里掷骰**——两段式
  玩家掷骰下，骰子由玩家在前端点击确认后才服务端权威生成，这里只把裁决
  产出的检定请求解析成待掷记录（`pending.PendingCheck`），真正的掷骰在
  `KeeperAgent.resolve_check` 里发生（见 agent.py）。
"""

import uuid

import structlog

from app.core.keeper.capabilities import executors
from app.core.keeper.decision import KeeperDecision
from app.core.keeper.deps import KeeperDeps, KeeperToolError, resolve_character
from app.core.keeper.location_state import location_of
from app.core.keeper.pending import PendingCheck
from app.core.keeper.skill_names import resolve_skill_id
from app.core.keeper.subject import KEEPER, Subject, authorize_decision, sanitize_decision
from app.core.keeper.tools import (
    _resolve_skill_target,
)

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
    for hook in executors():
        hook_report, hook_issues = await hook.run(deps, decision)
        report.extend(hook_report)
        issues.extend(hook_issues)

    if issues:
        logger.warning("keeper_decision_issues", issues=issues)
    return report, issues


async def create_pending_checks(
    deps: KeeperDeps, decision: KeeperDecision, *, subject: Subject = KEEPER
) -> tuple[list[PendingCheck], list[str]]:
    """把裁决里的 checks/san_checks 解析成待掷记录——**本函数不掷骰**。

    玩家/技能名的合法性预检复用 tools.py 内部的解析函数（跟 roll_check_impl/
    san_check_impl 走的是同一套解析逻辑，保证"能不能掷"的判断口径一致）；
    非法项跳过并记 issue（未知技能名、找不到的玩家），与旧版执行器行为一致。
    另：设计 02——当前场景节点若标注了 checks[]，只允许其中的 skill 进入
    check.request（第一层模组护栏）。
    返回 (待掷记录, 问题清单)。
    """
    from app.core.keeper.check_guard import filter_checks_against_module, find_node_for_scene
    from app.models.room import Room

    pending: list[PendingCheck] = []
    issues: list[str] = []

    # 发起检定同样是受权限管辖的动作（exec/14 P2）。无 REQUEST_CHECK /
    # REQUEST_SAN_CHECK 的主体连 schema 里都没有这两个字段，这里是第二道。
    decision = sanitize_decision(subject, decision)

    # 🔴 护栏与账本都按**掷骰的那个人所在的场景**判定（P5.2）。分头探索后
    # 房间不再只有一个"当前场景"——用房间级指针去卡在地下室那位的检定，
    # 会拿书房的 checks[] 去否掉地下室的合法检定。所以必须先把玩家解析出来
    # 拿到他的位置，再过护栏；顺序与 P5.2 之前相反。
    keeper_state: dict | None = None
    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room and isinstance(room.keeper_state, dict):
            keeper_state = room.keeper_state
    raw_scene = (keeper_state or {}).get("当前场景")
    current_scene = str(raw_scene) if raw_scene is not None else None

    async with deps.session_factory() as db:
        for check in decision.checks:
            # 技能指向 id 化（exec/17）：`skill_id` 应当是白名单里的技能 id 或
            # 属性 key。JSON mode 约束不到生成，所以模型仍可能写中文名——那条
            # 路径**保留但打点**（`_resolve_skill_target` 本来就同时认 id 和
            # 名字），日志能统计守规率，据此再决定要不要收紧成硬失败。
            # 不静默的意思是这里有 warning，不是"假装没发生"。
            if resolve_skill_id(deps.ruleset, check.skill_id) is None:
                logger.warning("keeper_skill_id_fallback", raw=check.skill_id)
            try:
                player, character = await resolve_character(db, deps, check.player)
                display_name, _target = _resolve_skill_target(deps, character, check.skill_id)
            except KeeperToolError as exc:
                issues.append(f"检定[{check.skill_id}]未能发起：{exc}")
                continue
            node_id = location_of(keeper_state, player.id)
            # 🔴 id vs id（exec/17 (A)）：模组数据组装期已归一成规则表 id，
            # 裁决器输出的也是 id，护栏是纯集合比较——运行时不再有同义词表。
            #
            # 🔴 动手那一轮**不过护栏**（exec/19 #49，试玩实测抓到的回归）：
            # 护栏（设计 02）防的是"玩家用模组没标注的调查技能即兴挖线索"
            # （拿克苏鲁神话看穿真相那种）。**战斗不属于这个范畴**——模组不可能
            # 在每个节点都标注格斗检定点，而玩家有权动手。
            #
            # 不豁免会死循环：护栏拦掉格斗 → 本轮零检定 → #44 的兜底要求叙事
            # 停下来追问 → 玩家再说一次"我砸他的头" → 又被拦 → 又追问。
            # 试玩里连着两轮都在问"你是要砸他的头？"，玩家永远打不出这一拳。
            if decision.player_state != "physical_conflict":
                kept, guard_issues = filter_checks_against_module(
                    deps.module,
                    [check.skill_id],
                    current_scene=current_scene,
                    current_node_id=node_id,
                )
                issues.extend(guard_issues)
            else:
                kept = [check.skill_id]
            if not kept:
                continue
            # 事实账本（exec/14 P4）：这名玩家所在节点上同名检定标注的
            # reveals，绑定到待掷记录上。查不到节点/查不到同名检定就是空。
            scene_node = find_node_for_scene(deps.module, current_scene, node_id=node_id)
            reveals: tuple[str, ...] = ()
            if scene_node is not None:
                for module_check in scene_node.checks:
                    if check.skill_id in module_check.skill_ids and module_check.reveals:
                        reveals = tuple(module_check.reveals)
                        break
            # 对抗检定（exec/19 #38）：目标值必须落在 1–100。越界就**跳过整条
            # 检定并记 issue**，不悄悄夹紧成 100——夹紧等于把裁决器写错的数字
            # 变成一个看似合理的规则结论，玩家和日志都看不出发生过什么。
            opposed_opponent: str | None = None
            opposed_value: int | None = None
            if check.opposed is not None:
                if not 1 <= check.opposed.value <= 100:
                    issues.append(
                        f"对抗检定[{check.skill_id}]未发起：对手目标值 {check.opposed.value} "
                        "不在 1-100（属性点要 ×5 换算成百分位）"
                    )
                    continue
                opposed_opponent = check.opposed.opponent
                opposed_value = check.opposed.value
            pending.append(
                PendingCheck(
                    check_request_id=str(uuid.uuid4()),
                    kind="skill",
                    room_id=deps.room_id,
                    player_id=player.id,
                    player_nickname=player.nickname,
                    skill=display_name,
                    loss_on_success="0",
                    loss_on_failure="0",
                    reason=check.reason,
                    reveals=reveals,
                    opposed_opponent=opposed_opponent,
                    opposed_value=opposed_value,
                )
            )
        for san in decision.san_checks:
            try:
                player, _character = await resolve_character(db, deps, san.player)
            except KeeperToolError as exc:
                issues.append(f"理智检定未能发起：{exc}")
                continue
            pending.append(
                PendingCheck(
                    check_request_id=str(uuid.uuid4()),
                    kind="san",
                    room_id=deps.room_id,
                    player_id=player.id,
                    player_nickname=player.nickname,
                    skill=None,
                    loss_on_success=san.loss_on_success,
                    loss_on_failure=san.loss_on_failure,
                    reason=san.reason,
                )
            )

    if issues:
        logger.warning("keeper_pending_check_issues", issues=issues)
    return pending, issues
