"""检定发起护栏（设计 02）：模组标注 checks 优先，发 check.request 前代码校验。

- 收集模组节点上标注的 skill 集合（含 sub_nodes）
- 定位当前节点：优先用结构化的场景节点 id（keeper_state 的「当前场景节点」，
  见 scene_state.py::CURRENT_NODE_KEY）做精确查找；id 缺失或未命中时，退回对
  「当前场景」自由文本人类地名做模糊匹配（兼容尚未产出 node id 的历史房间）。
- 若能定位到节点，且该节点标注了 checks：裁决给出的 skill 必须命中该节点
  checks（别名归一后）之一，否则丢弃并记 issue
- 若节点无 checks 或找不到当前节点：仅要求 skill 能被 ruleset 解析（由调用方
  turn_executor.py::create_pending_checks 已做）
"""

from __future__ import annotations

from app.core.keeper.module_loader import ModuleNode, ScenarioModule
from app.core.keeper.skill_names import match_key


def _norm_skill(name: str) -> str:
    """🔴 必须与执行层同一口径（`skill_names.match_key`）。

    此前这里只去空白+小写、**不做同义词归一**，于是模组标注「侦查」而裁决器
    按规范名发起「侦察」时被精确比较拦掉，玩家侧完全静默（exec/12 #32）。
    不变量：护栏不该拦住执行层能解析的技能名。
    """
    return match_key(name)


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
    skills: set[str] = set()
    for node in iter_nodes(module.nodes):
        for c in node.checks:
            if c.skill:
                skills.add(_norm_skill(c.skill))
    return skills


def find_node_for_scene(
    module: ScenarioModule,
    scene_hint: str | None,
    *,
    node_id: str | None = None,
) -> ModuleNode | None:
    """定位当前节点：结构化 node_id 精确查找优先，退回对「当前场景」自由
    文本人类地名做模糊匹配（兼容尚未产出 node id 的历史房间/模组）。"""
    if node_id:
        node = module.node_by_id(node_id)
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
    check_skills: list[str],
    *,
    current_scene: str | None,
    current_node_id: str | None = None,
) -> tuple[list[str], list[str]]:
    """返回 (保留的 skill 原名列表, issue 文案)。

    仅在「能定位到当前节点且节点有 checks[]」时强制命中模组标注；
    否则全部放行（即兴层，设计 02 第二层）。
    """
    node = find_node_for_scene(module, current_scene, node_id=current_node_id)
    if node is None or not node.checks:
        return list(check_skills), []

    allowed = {_norm_skill(c.skill) for c in node.checks if c.skill}
    # 节点写了 checks 但 skill 空 → 不挡
    if not allowed:
        return list(check_skills), []

    kept: list[str] = []
    issues: list[str] = []
    for skill in check_skills:
        if _norm_skill(skill) in allowed:
            kept.append(skill)
        else:
            issues.append(
                f"检定[{skill}]未发起：当前场景「{node.title}」模组标注检定点为"
                f"{sorted({c.skill for c in node.checks if c.skill})}，"
                "不允许即兴该技能（设计 02 第一层）"
            )
    return kept, issues
