"""组装产物的五项机械校验（预处理管线校验闭环）。

全部纯代码、不靠 LLM 自觉。与 docs/keeper-design/exec/05-… 第 3 节对齐：

1. schema 合法 —— ScenarioModule.model_validate
2. 引用闭合 —— leads_to / sub_node / branches 引用、id 唯一
3. 技能名可查 —— check.skill 能在 COC7 规则表解析（与 keeper tools 同源口径）
4. 无孤儿片段 —— 阶段 1 归组映射覆盖全部源片段 id
5. 不泄密 —— kp_truth.key_facts 关键词在 player_intro + opening.script 零出现
   （只查这两个玩家可见字段；node.kp_text 等本就是 KP 专用，不查）

脚本只吃结构化数据与映射，不内嵌任何模组正文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.coc7_content import build_coc7_ruleset
from app.core.keeper.module_loader import ModuleNode, ScenarioModule
from app.dto.game import RulesetRead

# 不泄密：从 key_facts 切出的关键词最短长度。太短（如「先生」）会误伤。
_MIN_KEYWORD_LEN = 4


@dataclass
class ValidationReport:
    """五项校验的汇总。ok=True 表示全部通过。"""

    ok: bool
    schema_ok: bool
    schema_errors: list[str] = field(default_factory=list)
    ref_ok: bool = True
    ref_errors: list[str] = field(default_factory=list)
    skill_ok: bool = True
    skill_errors: list[str] = field(default_factory=list)
    orphan_ok: bool = True
    orphan_errors: list[str] = field(default_factory=list)
    leak_ok: bool = True
    leak_errors: list[str] = field(default_factory=list)

    def all_errors(self) -> list[str]:
        out: list[str] = []
        out.extend(f"[schema] {e}" for e in self.schema_errors)
        out.extend(f"[ref] {e}" for e in self.ref_errors)
        out.extend(f"[skill] {e}" for e in self.skill_errors)
        out.extend(f"[orphan] {e}" for e in self.orphan_errors)
        out.extend(f"[leak] {e}" for e in self.leak_errors)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "schema": {"ok": self.schema_ok, "errors": self.schema_errors},
            "ref": {"ok": self.ref_ok, "errors": self.ref_errors},
            "skill": {"ok": self.skill_ok, "errors": self.skill_errors},
            "orphan": {"ok": self.orphan_ok, "errors": self.orphan_errors},
            "leak": {"ok": self.leak_ok, "errors": self.leak_errors},
            "all_errors": self.all_errors(),
        }

    def summary_text(self) -> str:
        lines = [
            f"校验总结果：{'通过' if self.ok else '失败'}",
            f"  schema 合法：{'✓' if self.schema_ok else '✗'} ({len(self.schema_errors)} 条)",
            f"  引用闭合：{'✓' if self.ref_ok else '✗'} ({len(self.ref_errors)} 条)",
            f"  技能名可查：{'✓' if self.skill_ok else '✗'} ({len(self.skill_errors)} 条)",
            f"  无孤儿片段：{'✓' if self.orphan_ok else '✗'} ({len(self.orphan_errors)} 条)",
            f"  不泄密：{'✓' if self.leak_ok else '✗'} ({len(self.leak_errors)} 条)",
        ]
        if self.all_errors():
            lines.append("错误清单：")
            for e in self.all_errors():
                lines.append(f"  - {e}")
        return "\n".join(lines)


def skill_resolvable(skill_name: str, ruleset: RulesetRead) -> bool:
    """与 app.core.keeper.tools._resolve_skill_target 同源口径（不依赖角色卡）。

    支持：技能中文名 / 技能 id / 英文名 / 属性中文名或缩写；
    「侦查」归一到「侦察」。
    """
    wanted = skill_name.strip().replace("侦查", "侦察")
    if not wanted:
        return False
    for attr in ruleset.attributes:
        if wanted in (attr.key, attr.label):
            return True
    for spec in ruleset.skills:
        if wanted in (spec.id, spec.name) or (
            spec.name_en is not None and wanted.lower() == spec.name_en.lower()
        ):
            return True
    return False


def _collect_node_ids(nodes: list[ModuleNode]) -> set[str]:
    ids: set[str] = set()
    for node in nodes:
        ids.add(node.id)
        if node.sub_node is not None:
            ids.add(node.sub_node.id)
    return ids


def _iter_nodes(nodes: list[ModuleNode]) -> list[ModuleNode]:
    out: list[ModuleNode] = []
    for node in nodes:
        out.append(node)
        if node.sub_node is not None:
            out.append(node.sub_node)
    return out


def check_schema(raw: dict[str, Any]) -> tuple[ScenarioModule | None, list[str]]:
    try:
        mod = ScenarioModule.model_validate(raw)
        return mod, []
    except Exception as exc:  # noqa: BLE001 — 校验要记完整错误
        return None, [str(exc)]


def check_refs(module: ScenarioModule) -> list[str]:
    errors: list[str] = []
    node_ids = _collect_node_ids(module.nodes)
    # id 唯一：node / npc / ending / agenda
    for label, ids in (
        ("node", [n.id for n in _iter_nodes(module.nodes)]),
        ("npc", [n.id for n in module.npcs]),
        ("ending", [e.id for e in module.endings]),
        ("agenda", [a.id for a in module.agenda]),
    ):
        seen: set[str] = set()
        for i in ids:
            if i in seen:
                errors.append(f"{label} id 重复：{i!r}")
            seen.add(i)

    for node in _iter_nodes(module.nodes):
        for target in node.leads_to:
            if target not in node_ids:
                errors.append(f"node {node.id!r} leads_to 悬空：{target!r}")
        if node.sub_node is not None and node.sub_node.id not in node_ids:
            # sub_node 自己也在集合里，这里只是防御
            errors.append(f"node {node.id!r} sub_node.id 未登记：{node.sub_node.id!r}")
        # branches 里的 then 不引用 node id（outcome 是自由文本）；
        # 若 outcome/condition 里看起来像 id 引用，留给内容层，骨架层不扫文本。
    return errors


def check_skills(module: ScenarioModule, ruleset: RulesetRead | None = None) -> list[str]:
    ruleset = ruleset or build_coc7_ruleset()
    errors: list[str] = []
    for node in _iter_nodes(module.nodes):
        for i, check in enumerate(node.checks):
            if not skill_resolvable(check.skill, ruleset):
                errors.append(f"node {node.id!r} checks[{i}].skill 无法解析：{check.skill!r}")
    return errors


def check_orphans(
    source_item_ids: set[str],
    assignment_map: dict[str, Any],
) -> list[str]:
    """assignment_map: item_id -> 归宿描述（任意非空即可算有归宿）。

    阶段 1 把每个片段写进映射；未出现在映射里的 = 孤儿。
    映射值可以是字符串归宿，也可以是含 dest 的 dict。
    """
    errors: list[str] = []
    covered = set(assignment_map.keys())
    missing = sorted(source_item_ids - covered)
    for mid in missing:
        errors.append(f"源片段未归组：{mid!r}")
    # 映射里多出来的 id 不算孤儿（可能是幻觉 id），单独警告
    extra = sorted(covered - source_item_ids)
    for eid in extra:
        errors.append(f"归组映射出现未知片段 id：{eid!r}")
    return errors


_SPLIT_RE = re.compile(r"[，。；、,\s;：:（）()「」\"'【】\[\]《》·…—\-_/\\|]+")


def extract_secret_keywords(key_facts: list[str]) -> list[str]:
    """从 key_facts 抽出用于泄密扫描的关键词（机械，不靠 LLM）。

    - 整条 fact（长度在阈值以上且不太长）本身是关键词
    - 按标点切开后长度 ≥ _MIN_KEYWORD_LEN 的片段也是
    """
    found: list[str] = []
    seen: set[str] = set()
    for fact in key_facts:
        s = str(fact).strip()
        if not s:
            continue
        candidates = [s] if _MIN_KEYWORD_LEN <= len(s) <= 80 else []
        candidates.extend(p for p in _SPLIT_RE.split(s) if len(p) >= _MIN_KEYWORD_LEN)
        for c in candidates:
            if c not in seen:
                seen.add(c)
                found.append(c)
    return found


def check_leak(module: ScenarioModule) -> list[str]:
    """只扫 player_intro 与 opening.script——唯一直接给玩家的字段。"""
    player_texts: list[tuple[str, str]] = [("player_intro", module.player_intro or "")]
    if module.opening is not None and module.opening.script:
        player_texts.append(("opening.script", module.opening.script))

    keywords = extract_secret_keywords(list(module.kp_truth.key_facts or []))
    errors: list[str] = []
    for kw in keywords:
        for field_name, text in player_texts:
            if kw and kw in text:
                errors.append(f"真相关键词 {kw!r} 出现在玩家可见字段 {field_name}")
    return errors


def validate_assembled(
    raw: dict[str, Any],
    *,
    source_item_ids: set[str],
    assignment_map: dict[str, Any],
    ruleset: RulesetRead | None = None,
) -> ValidationReport:
    """跑完全部五项，返回报告。schema 失败时后续项尽量仍扫原始 dict。"""
    ruleset = ruleset or build_coc7_ruleset()
    module, schema_errors = check_schema(raw)
    schema_ok = module is not None and not schema_errors

    ref_errors: list[str] = []
    skill_errors: list[str] = []
    leak_errors: list[str] = []
    if module is not None:
        ref_errors = check_refs(module)
        skill_errors = check_skills(module, ruleset)
        leak_errors = check_leak(module)
    else:
        # schema 都过不了时，仍给一点可修线索
        schema_errors = schema_errors or ["ScenarioModule.model_validate 失败"]

    orphan_errors = check_orphans(source_item_ids, assignment_map)

    ref_ok = not ref_errors
    skill_ok = not skill_errors
    orphan_ok = not orphan_errors
    leak_ok = not leak_errors
    ok = schema_ok and ref_ok and skill_ok and orphan_ok and leak_ok
    return ValidationReport(
        ok=ok,
        schema_ok=schema_ok,
        schema_errors=schema_errors,
        ref_ok=ref_ok,
        ref_errors=ref_errors,
        skill_ok=skill_ok,
        skill_errors=skill_errors,
        orphan_ok=orphan_ok,
        orphan_errors=orphan_errors,
        leak_ok=leak_ok,
        leak_errors=leak_errors,
    )
