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

from app.core.keeper.decision import KeeperDecision
from app.core.keeper.location_state import location_of
from app.core.keeper.pending import PendingCheck
from app.core.keeper.phase import PHASE_FINISHED, PHASE_INVESTIGATION
from app.core.keeper.skill_names import resolve_skill_id
from app.core.keeper.subject import KEEPER, Subject, authorize_decision, sanitize_decision
from app.core.keeper.tools import (
    KeeperDeps,
    KeeperToolError,
    _resolve_character,
    _resolve_skill_target,
    adjust_hp_impl,
    adjust_npc_hp_impl,
    clear_current_node_impl,
    mark_agenda_fired_impl,
    mark_visibility_revealed_impl,
    move_player_impl,
    set_current_node_impl,
    set_phase_impl,
    set_stealth_impl,
    update_state_impl,
)

logger = structlog.get_logger()


#: `state_updates` 里那个人类可读的场景键。它和 `current_node_id` 是同一件事的
#: 两个面（人读的地名 / 机器读的节点 id），两者脱节就会出 #48。
_SCENE_KEY = "当前场景"


def _scene_moved_off_the_map(decision: KeeperDecision) -> bool:
    """本轮换了场景，却没给出任何剧本节点 id（exec/19 #48）。

    只在裁决器**明确写了新的「当前场景」**时才成立——没提场景的普通轮次
    （对话、检定结算）不该动节点指针。
    """
    if decision.current_node_id:
        return False
    return any(u.key == _SCENE_KEY and u.value.strip() for u in decision.state_updates)


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

    议程 once 去重下沉在 mark_agenda_fired_impl（它拿得到 deps.module 与现值），
    这里只做「id 不存在 → issue」。

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

    for hp in decision.hp_changes:
        try:
            reason = hp.reason or "守秘人裁定"
            # NPC 与调查员走两条记账（exec/19 #39）：NPC 的状态挂在 keeper_state
            # 的 NPC 状态表上，没有角色卡可写。`npc` 优先——两个字段都填时以
            # 显式的 NPC 为准，因为 `player` 的默认语义是"本轮发起者"。
            if hp.npc:
                report.append(await adjust_npc_hp_impl(deps, hp.delta, reason, hp.npc))
            else:
                report.append(await adjust_hp_impl(deps, hp.delta, reason, hp.player))
        except KeeperToolError as exc:
            issues.append(f"HP 变更未执行：{exc}")
    for update in decision.state_updates:
        try:
            line, issue = await update_state_impl(deps, update.key, update.value, update.subject)
            report.append(line)
            if issue is not None:
                issues.append(issue)
        except KeeperToolError as exc:
            issues.append(f"状态更新未执行：{exc}")

    # 场景指针结构化（04 遗留项）：node_id 存在性由 set_current_node_impl
    # 校验（module.node_by_id）——非法 id 不写入、记为 issue，不炸整轮。
    if decision.current_node_id:
        try:
            report.append(await set_current_node_impl(deps, decision.current_node_id))
        except KeeperToolError as exc:
            issues.append(f"场景定位未执行：{exc}")
    elif _scene_moved_off_the_map(decision):
        # 🔴 场景变了、但没有任何剧本节点对应得上（exec/19 #48）。
        #
        # 试玩实测：终局「当前场景 = 科比特家门外（警察到场）」，而节点指针还
        # 停在 basement-laboratory——玩家已经站在屋外，护栏却拿地下室的 checks[]
        # 去卡他的检定。裁决器**做对了**（找不到对应节点就留空，不编造 id），
        # 错在代码把"没说"当成了"没变"。
        #
        # 正确语义是**清空**：人在剧本节点之外的地方，护栏退化到即兴层放行。
        # 与 #37 同族——空间状态是地基，宁可承认不知道，不可拿旧值硬撑。
        try:
            cleared = await clear_current_node_impl(deps)
            if cleared:
                report.append(cleared)
        except KeeperToolError as exc:
            issues.append(f"场景指针清空未执行：{exc}")

    # 分头探索（P5.2）：逐人覆盖，必须排在 current_node_id 之后——那个是
    # "本轮发言者的默认落点"，这个是"谁不跟大家一起"，顺序反了会被默认值盖掉。
    for move in decision.moves:
        try:
            report.append(await move_player_impl(deps, move.player, move.node_id))
        except KeeperToolError as exc:
            issues.append(f"分头移动未执行：{exc}")

    # 潜行状态（exec/18 ②）：与移动同一类空间状态，逐条执行、逐条记 issue。
    for change in decision.stealth:
        try:
            report.append(await set_stealth_impl(deps, change.player, change.hidden))
        except KeeperToolError as exc:
            issues.append(f"潜行状态未执行：{exc}")

    # 议程触发：只校验 id 合法性，once 幂等由 mark_agenda_fired_impl 兜底。
    if decision.agenda_fired:
        valid_ids: list[str] = []
        for eid in decision.agenda_fired:
            if deps.module.agenda_by_id(eid) is None:
                issues.append(f"议程事件未执行：剧本里没有 id={eid}")
                continue
            valid_ids.append(eid)
        if valid_ids:
            try:
                report.append(await mark_agenda_fired_impl(deps, valid_ids))
            except KeeperToolError as exc:
                issues.append(f"议程事件未执行：{exc}")

    # 密级配对揭开（路线 5）
    if decision.visibility_revealed:
        pair_ids_ok = {p.id for p in deps.module.visibility_pairs}
        valid_pairs: list[str] = []
        for pid in decision.visibility_revealed:
            if pid not in pair_ids_ok:
                issues.append(f"密级揭开未执行：剧本里没有 pair id={pid}")
                continue
            valid_pairs.append(pid)
        if valid_pairs:
            try:
                report.append(await mark_visibility_revealed_impl(deps, valid_pairs))
            except KeeperToolError as exc:
                issues.append(f"密级揭开未执行：{exc}")

    # 对局阶段推进（路线 6）
    if decision.ending_reached:
        eid = decision.ending_reached
        if deps.module.endings and not any(e.id == eid for e in deps.module.endings):
            issues.append(f"结局收束未执行：剧本里没有 ending id={eid}")
        else:
            try:
                # 收束当轮直接 finished：叙事仍可写终章，下一行动立即拒
                report.append(await set_phase_impl(deps, PHASE_FINISHED, ending_id=eid))
            except KeeperToolError as exc:
                issues.append(f"结局收束未执行：{exc}")
    elif decision.opening_complete:
        try:
            report.append(await set_phase_impl(deps, PHASE_INVESTIGATION))
        except KeeperToolError as exc:
            issues.append(f"开场完成未执行：{exc}")

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
                player, character = await _resolve_character(db, deps, check.player)
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
                player, _character = await _resolve_character(db, deps, san.player)
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
