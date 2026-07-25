"""检定发起护栏（设计 02）：模组标注 checks 优先，发 check.request 前代码校验。

- 收集模组节点上标注的 skill 集合（含 sub_nodes）
- 若 keeper_state 有「当前场景」且能匹配到节点，且该节点标注了 checks：
  裁决给出的 skill 必须命中该节点 checks（别名归一后）之一，否则丢弃并记 issue
- 若节点无 checks 或找不到当前节点：仅要求 skill 能被 ruleset 解析（由调用方
  create_pending_checks 已做）
"""

from __future__ import annotations

from app.core.keeper.module_loader import ModuleNode, ScenarioModule


def _norm_skill(name: str) -> str:
    return (name or "").strip().replace(" ", "").lower()


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


def find_node_for_scene(module: ScenarioModule, scene_hint: str | None) -> ModuleNode | None:
    """用 keeper_state 的「当前场景」对节点 id / title 做模糊匹配。"""
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
) -> tuple[list[str], list[str]]:
    """返回 (保留的 skill 原名列表, issue 文案)。

    仅在「当前场景能匹配到节点且节点有 checks[]」时强制命中模组标注；
    否则全部放行（即兴层，设计 02 第二层）。
    """
    node = find_node_for_scene(module, current_scene)
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
