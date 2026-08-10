"""模组标注的理智检定点：局面块 + 已触发记账。

## 🔴 这是**反向**护栏，而且它只能是提醒

`skill_check` 的护栏管的是「模组没标注的不许掷」；反过来「模组标注了却一次
都没触发」此前没有任何东西管。真人实测（`exec/31 #73`）：导入的模组 23 个
节点里只有 1 处 `kind="san"` 检定点，玩家**进去了**，裁决器当轮只发了一次
逃跑的敏捷对抗，SAN 一次没起——全局唯一那次理智检定反而掷在没标注的地方。

**执行侧刻意不做成代码强制掷。** COC7 里触发点是「目睹的那一刻」，代码判得了
「人在这个节点」，判不了「他看见了没有」；而 SAN 写进角色卡不可撤回，掷早了
还会撞上规则里「同一来源不重复检定」。所以按 `exec/20` 的分层，这一条是
**触发条件代码判、执行方式发请求 → 概率性改进**，汇报时说"已改善"，不说"已修复"。

已触发记账（`SAN_POINTS_FIRED_KEY`）存在的理由也在这里：没有它，玩家在这个
节点待几轮就会被提醒几轮，模型照做就是重复扣 SAN——比不提醒更糟。
"""

from __future__ import annotations

from app.core.keeper.contract.module_loader import ModuleCheck, ScenarioModule
from app.core.keeper.contract.registry import SituationContext
from app.core.keeper.runtime.location_state import location_of, resolve_content_node_id
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY

SAN_POINTS_FIRED_KEY = "已触发理智检定点"

#: 模组里表示"这条检定点是理智检定"的 kind（`ModuleCheck.kind` 的封闭取值之一）。
SAN_KIND = "san"


def san_point_ref(node_id: str, index: int) -> str:
    """一处理智检定点的引用。

    🔴 `ModuleCheck` 没有 id，只能用「节点 id + 它在 checks 里的序号」定位。
    这不是"自由文本当标识符"——两半都是代码算出来的，模型碰不到它。
    """
    return f"{node_id}#{index}"


def load_fired_san_points(keeper_state: dict | None) -> list[str]:
    """解析已触发过的检定点引用（存储形态同 `已触发议程`：逗号分隔字符串）。"""
    if not keeper_state:
        return []
    raw = keeper_state.get(SAN_POINTS_FIRED_KEY)
    if raw is None or raw == "":
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def occupied_node_ids(
    module: ScenarioModule,
    keeper_state: dict | None,
    players: tuple[tuple[str, str], ...],
) -> list[str]:
    """调查员此刻所在的剧本节点（保序去重）。

    分头时是多个。谁都定位不到（人在剧本节点之外）就是空列表——那时本来也
    没有"模组标注的检定点"可言，与护栏的退化口径一致。

    即兴地点沿 `from` 上溯（`resolve_content_node_id`）：站在「屋后」的人
    照样看得见「科比特家」标注的理智检定点，否则一旦分头到即兴位置，这块
    反向护栏就整个失效。
    """
    found: list[str] = []
    seen: set[str] = set()
    candidates = [
        resolve_content_node_id(module, keeper_state, location_of(keeper_state, pid))
        for pid, _name in players
    ]
    if not players:
        candidates = [
            resolve_content_node_id(
                module, keeper_state, (keeper_state or {}).get(CURRENT_NODE_KEY)
            )
        ]
    for node_id in candidates:
        if node_id and node_id not in seen:
            seen.add(node_id)
            found.append(node_id)
    return found


def san_points_at(
    module: ScenarioModule, node_id: str, fired: set[str]
) -> list[tuple[int, ModuleCheck]]:
    """这个节点上还没触发过的理智检定点。节点不存在 → 空列表。"""
    node = module.node_by_id(node_id)
    if node is None:
        return []
    return [
        (index, check)
        for index, check in enumerate(node.checks)
        if check.kind == SAN_KIND and san_point_ref(node_id, index) not in fired
    ]


def fired_refs_at(module: ScenarioModule, node_id: str) -> list[str]:
    """这个节点上全部理智检定点的引用——本轮真发起了检定就把它们记掉。"""
    node = module.node_by_id(node_id)
    if node is None:
        return []
    return [
        san_point_ref(node_id, index)
        for index, check in enumerate(node.checks)
        if check.kind == SAN_KIND
    ]


def format_san_points(
    module: ScenarioModule,
    keeper_state: dict | None,
    players: tuple[tuple[str, str], ...],
) -> str:
    """局面块正文。没有待触发的检定点就返回空串——整块不渲染。"""
    fired = set(load_fired_san_points(keeper_state))
    lines: list[str] = []
    for node_id in occupied_node_ids(module, keeper_state, players):
        node = module.node_by_id(node_id)
        if node is None:
            continue
        for _index, check in san_points_at(module, node_id, fired):
            difficulty = f"（{check.difficulty}）" if check.difficulty else ""
            lines.append(
                f"- 「{node.title}」{difficulty}：成功损失 {check.on_success or '—'}／"
                f"失败损失 {check.on_failure or '—'}"
            )
    if not lines:
        return ""
    return (
        "调查员此刻所在的节点上，模组标注了下面这些理智检定点，本局还没掷过。\n"
        "玩家在这一轮**目睹**对应之物时，必须在 `san_checks` 里发起，"
        "`loss_on_success`/`loss_on_failure` 照抄下面的数值；还没看见就先别掷。\n"
        + "\n".join(lines)
    )


def render_san_points(context: SituationContext) -> str:
    """注册进局面块的 situation 钩子。"""
    return format_san_points(context.module, context.keeper_state, context.players)
