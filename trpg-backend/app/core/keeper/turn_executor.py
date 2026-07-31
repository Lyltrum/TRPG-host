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
from app.core.keeper.pending import PendingCheck
from app.core.keeper.phase import PHASE_FINISHED, PHASE_INVESTIGATION
from app.core.keeper.scene_state import load_current_node_id
from app.core.keeper.subject import KEEPER, Subject, authorize_decision, sanitize_decision
from app.core.keeper.tools import (
    KeeperDeps,
    KeeperToolError,
    _resolve_character,
    _resolve_skill_target,
    adjust_hp_impl,
    mark_agenda_fired_impl,
    mark_visibility_revealed_impl,
    set_current_node_impl,
    set_phase_impl,
    update_state_impl,
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
            report.append(
                await adjust_hp_impl(deps, hp.delta, hp.reason or "守秘人裁定", hp.player)
            )
        except KeeperToolError as exc:
            issues.append(f"HP 变更未执行：{exc}")
    for update in decision.state_updates:
        try:
            report.append(await update_state_impl(deps, update.key, update.value))
        except KeeperToolError as exc:
            issues.append(f"状态更新未执行：{exc}")

    # 场景指针结构化（04 遗留项）：node_id 存在性由 set_current_node_impl
    # 校验（module.node_by_id）——非法 id 不写入、记为 issue，不炸整轮。
    if decision.current_node_id:
        try:
            report.append(await set_current_node_impl(deps, decision.current_node_id))
        except KeeperToolError as exc:
            issues.append(f"场景定位未执行：{exc}")

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
    from app.core.keeper.check_guard import filter_checks_against_module
    from app.models.room import Room

    pending: list[PendingCheck] = []
    issues: list[str] = []

    # 发起检定同样是受权限管辖的动作（exec/14 P2）。无 REQUEST_CHECK /
    # REQUEST_SAN_CHECK 的主体连 schema 里都没有这两个字段，这里是第二道。
    decision = sanitize_decision(subject, decision)

    # 模组节点护栏：先按当前场景过滤 skill 列表（node_id 精确定位优先，
    # 自由文本地名模糊匹配兜底——见 check_guard.find_node_for_scene）。
    current_scene: str | None = None
    current_node_id: str | None = None
    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room and isinstance(room.keeper_state, dict):
            raw = room.keeper_state.get("当前场景")
            current_scene = str(raw) if raw is not None else None
            current_node_id = load_current_node_id(room.keeper_state)
    allowed_skills, guard_issues = filter_checks_against_module(
        deps.module,
        [c.skill for c in decision.checks],
        current_scene=current_scene,
        current_node_id=current_node_id,
    )
    issues.extend(guard_issues)
    allowed_set = set(allowed_skills)
    # 保持原 checks 顺序，只保留通过护栏的
    guarded_checks = [c for c in decision.checks if c.skill in allowed_set]

    async with deps.session_factory() as db:
        for check in guarded_checks:
            try:
                player, character = await _resolve_character(db, deps, check.player)
                display_name, _target = _resolve_skill_target(deps, character, check.skill)
            except KeeperToolError as exc:
                issues.append(f"检定[{check.skill}]未能发起：{exc}")
                continue
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
