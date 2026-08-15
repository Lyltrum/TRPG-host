"""检定发起护栏（设计 02）：模组标注 checks 优先，发 check.request 前代码校验。

- 收集模组节点上标注的技能 **id** 集合（含 sub_nodes）
- 定位当前节点：优先用结构化的场景节点 id（keeper_state 的「当前场景节点」，
  见 scene_state.py::CURRENT_NODE_KEY）做精确查找；id 缺失或未命中时，退回对
  「当前场景」自由文本人类地名做模糊匹配（兼容尚未产出 node id 的历史房间）。
- 若能定位到节点，且该节点标注了 checks：裁决给出的 skill_id 必须命中该节点
  checks 的 skill_ids 之一，否则丢弃并记 issue
- 若节点无 checks 或找不到当前节点：仅要求 skill_id 能被 ruleset 解析（由调用方
  turn_executor.py::create_pending_checks 已做）

exec/17 (A) 起两侧都是 id，本模块**不再做任何技能名归一**——模组数据在
组装期就归一好了，运行时没有字符串匹配可言。
"""

from __future__ import annotations

from app.core.keeper.contract.module_loader import ModuleNode, ScenarioModule
from app.core.keeper.runtime.location_state import resolve_content_node_id


def iter_nodes(nodes: list[ModuleNode]) -> list[ModuleNode]:
    out: list[ModuleNode] = []
    for n in nodes:
        out.append(n)
        if n.sub_node is not None:
            out.extend(iter_nodes([n.sub_node]))
        if n.sub_nodes:
            out.extend(iter_nodes(n.sub_nodes))
    return out


def collect_module_check_skills(module: ScenarioModule) -> set[str]:
    """全模组标注过的技能 id 集合（exec/17 (A) 起是 id，不再是归一后的名字）。"""
    skills: set[str] = set()
    for node in iter_nodes(module.nodes):
        for c in node.checks:
            skills.update(c.skill_ids)
    return skills


def find_node_for_scene(
    module: ScenarioModule,
    scene_hint: str | None,
    *,
    node_id: str | None = None,
    keeper_state: dict | None = None,
) -> ModuleNode | None:
    """定位当前节点：结构化 node_id 精确查找优先，退回对「当前场景」自由
    文本人类地名做模糊匹配（兼容尚未产出 node id 的历史房间/模组）。

    `keeper_state` 非空时，即兴地点（`loc-N`）沿 `from` 链上溯到它派生自的
    剧本节点——站在「科比特家屋后」的人读得到「科比特家」的内容。理由见
    `location_state.resolve_content_node_id`。不传就退化成原来的行为。
    """
    if node_id:
        resolved = (
            resolve_content_node_id(module, keeper_state, node_id)
            if keeper_state is not None
            else node_id
        )
        node = module.node_by_id(resolved) if resolved else None
        if node is not None:
            return node
    if not scene_hint or not scene_hint.strip():
        return None
    hint = scene_hint.strip()
    hint_l = hint.lower()
    all_nodes = iter_nodes(module.nodes)
    for n in all_nodes:
        if n.id == hint or n.title == hint:
            return n
    for n in all_nodes:
        if hint in n.title or n.title in hint or hint_l in n.id.lower():
            return n
    return None


def filter_checks_against_module(
    module: ScenarioModule,
    check_skill_ids: list[str],
    *,
    current_scene: str | None,
    current_node_id: str | None = None,
    keeper_state: dict | None = None,
) -> tuple[list[str], list[str]]:
    """返回 (**有揭示权**的 skill_id 列表, issue 文案)。

    🔴 **2026-08-15 起这个函数不再决定"掷不掷"，只决定"揭不揭得开"。**
    调用方（`skill_check/executor.py::create_pending_skill_checks`）照常为每条
    检定建待掷记录，只把没通过的那些的 `reveals` 置空。函数名与返回形状没动
    ——变的是调用方怎么用它。理由与实据写在调用点，一句话版本：**要防的是
    "即兴掷一把就把模组真相挖出来"，不是"不许即兴掷骰"**。

    仅在「能定位到当前节点且节点有可用的 checks[]」时强制命中模组标注；
    否则全部放行（即兴层，设计 02 第二层）。

    🔴 两侧都是 **id**（exec/17 (A)）：模组数据在组装期已归一成规则表 id，
    裁决器输出的也是 id，所以这里是集合比较，不再有任何字符串归一。
    此前两侧都是自由文本，才需要一张同义词表勉强对齐——那正是 exec/12 #32
    「模组写侦查、裁决器写侦察、护栏精确比较拦掉、玩家侧完全静默」的病根。

    模组里一条检定点可以带**多个** id（"话术/魅惑/信用"这类多选检定点）：
    任一命中即放行。`kind="san"` 的检定点不指向技能，不参与这里的白名单。
    """
    node = find_node_for_scene(
        module, current_scene, node_id=current_node_id, keeper_state=keeper_state
    )
    if node is None or not node.checks:
        return list(check_skill_ids), []

    allowed: set[str] = set()
    for c in node.checks:
        if c.kind == "san":
            continue
        allowed.update(c.skill_ids)
    # 节点写了 checks 但没有任何可用 id（未归一的老数据 / 纯理智检定）→ 不挡。
    # 这是**显式降级**：没有机器可读的白名单就没有可执行的限制，不是兜底猜测。
    if not allowed:
        return list(check_skill_ids), []

    kept: list[str] = []
    issues: list[str] = []
    for skill_id in check_skill_ids:
        if skill_id in allowed:
            kept.append(skill_id)
        else:
            issues.append(
                f"检定[{skill_id}]照常掷出，但揭不开模组事实：当前场景"
                f"「{node.title}」标注的检定点是 {sorted(allowed)}，"
                f"这一条属于即兴（设计 02 第一层）"
            )
    return kept, issues
