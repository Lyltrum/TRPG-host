"""组装产物的机械校验（预处理管线校验闭环）。

与 docs/keeper-design/exec/05 第 3 节 + 06 第 2–3 节对齐：

原五项（硬失败）：
1. schema 合法 —— ScenarioModule.model_validate
2. 引用闭合 —— leads_to / sub_node / branches 引用、id 唯一
3. 技能名可查 —— check.skill 能在 COC7 规则表解析（与 keeper tools 同源口径）
4. 无孤儿片段 —— 阶段 1 归组映射覆盖全部源片段 id
5. 不泄密 —— kp_truth.key_facts 关键词在 player_intro + opening.script 零出现
   （只查这两个玩家可见字段；node.kp_text 等本就是 KP 专用，不查）

06 新增三项（硬失败）：
6. 薄公开槽上限 —— player_intro / opening / meta 各 ≤1 片段，合计 ≤3
7. 绝密不进公开槽 —— audience 含绝密/守密人/KP 的片段不得归到 player_intro/opening
8. 结构完整性 —— 源片段 what_kind 带结局/议程信号时，最终 endings/agenda 不得为空
   （完整性检查，不是路由逻辑；信号取自片段自身 what_kind_of_thing）

06 内容保全（软，不硬失败）：
- 归宿字段非空 + summary 关键词在对应实体文本中的命中率
- 低于阈值的片段列入「疑似内容丢失」清单，写入报告供人看

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

# 薄公开槽：阶段1 归宿 dest_kind
_THIN_PUBLIC_KINDS = ("player_intro", "opening", "meta")
_PUBLIC_SLOT_KINDS = ("player_intro", "opening")  # 绝密不得进入的槽

# 结构完整性：用片段自己的 what_kind_of_thing 子串当信号（非 COC 专属路由）
_ENDING_KIND_SIGNALS = ("结局", "结尾", "结束")
_AGENDA_KIND_SIGNALS = ("当前事件", "行动规律", "时间压力", "今晚", "期限")

# 内容保全：summary 关键词在目标文本中的最低命中率（宽松，抓整段蒸发）
# 多片段合一的 node/npc 由 LLM 改写，关键词噪声大——只对「单片段实体 + 薄字段」做命中率
_PRESERVE_HIT_RATIO = 0.15
_PRESERVE_MIN_KW_LEN = 2

# 组装层常见技能别名 → COC7 规则表可解析名（机械归一，不靠 LLM）
SKILL_ALIASES: dict[str, str] = {
    "侦查": "侦察",
    "驾驶汽车": "汽车驾驶",
    "驾车": "汽车驾驶",
    "开车": "汽车驾驶",
    "Drive Auto": "汽车驾驶",
    "drive auto": "汽车驾驶",
    "躲藏": "潜行",
    "隐蔽": "潜行",
    "植物学": "科学：植物学",
    "药剂学": "科学：药学",
    "药学": "科学：药学",
    "生物学": "科学：生物学",
    "化学": "科学：化学",
    "物理学": "科学：物理学",
    "天文学": "科学：天文学",
    "地质学": "科学：地质学",
    "斗殴": "格斗：斗殴",
    "手枪": "射击：手枪",
    "步枪": "射击：步枪/霰弹枪",
    "霰弹枪": "射击：步枪/霰弹枪",
    "图书馆": "图书馆使用",
    "图书馆使用检定": "图书馆使用",
}


@dataclass
class ContentPreserveItem:
    """单条疑似内容丢失。"""

    item_id: str
    dest_kind: str
    dest_id: str
    reason: str
    hit_ratio: float | None = None
    keywords_total: int = 0
    keywords_hit: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "dest_kind": self.dest_kind,
            "dest_id": self.dest_id,
            "reason": self.reason,
            "hit_ratio": self.hit_ratio,
            "keywords_total": self.keywords_total,
            "keywords_hit": self.keywords_hit,
        }


@dataclass
class ValidationReport:
    """校验汇总。ok=True 表示全部硬校验通过（内容保全软项不进 ok）。"""

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
    # 06 新增硬项
    thin_slot_ok: bool = True
    thin_slot_errors: list[str] = field(default_factory=list)
    secret_public_ok: bool = True
    secret_public_errors: list[str] = field(default_factory=list)
    structure_ok: bool = True
    structure_errors: list[str] = field(default_factory=list)
    # 06 内容保全（软）
    content_preserve_ok: bool = True  # 仅作观察；不参与 ok
    content_preserve_suspects: list[ContentPreserveItem] = field(default_factory=list)

    def all_errors(self) -> list[str]:
        out: list[str] = []
        out.extend(f"[schema] {e}" for e in self.schema_errors)
        out.extend(f"[ref] {e}" for e in self.ref_errors)
        out.extend(f"[skill] {e}" for e in self.skill_errors)
        out.extend(f"[orphan] {e}" for e in self.orphan_errors)
        out.extend(f"[leak] {e}" for e in self.leak_errors)
        out.extend(f"[thin_slot] {e}" for e in self.thin_slot_errors)
        out.extend(f"[secret_public] {e}" for e in self.secret_public_errors)
        out.extend(f"[structure] {e}" for e in self.structure_errors)
        return out

    def needs_stage1_repair(self) -> bool:
        """这些失败根因在归组映射，必须回灌阶段1，不能只改最终 JSON。"""
        return not (
            self.orphan_ok and self.thin_slot_ok and self.secret_public_ok and self.structure_ok
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "schema": {"ok": self.schema_ok, "errors": self.schema_errors},
            "ref": {"ok": self.ref_ok, "errors": self.ref_errors},
            "skill": {"ok": self.skill_ok, "errors": self.skill_errors},
            "orphan": {"ok": self.orphan_ok, "errors": self.orphan_errors},
            "leak": {"ok": self.leak_ok, "errors": self.leak_errors},
            "thin_slot": {"ok": self.thin_slot_ok, "errors": self.thin_slot_errors},
            "secret_public": {
                "ok": self.secret_public_ok,
                "errors": self.secret_public_errors,
            },
            "structure": {"ok": self.structure_ok, "errors": self.structure_errors},
            "content_preserve": {
                "ok": self.content_preserve_ok,
                "suspect_count": len(self.content_preserve_suspects),
                "suspects": [s.to_dict() for s in self.content_preserve_suspects],
            },
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
            (
                f"  薄公开槽上限：{'✓' if self.thin_slot_ok else '✗'} "
                f"({len(self.thin_slot_errors)} 条)"
            ),
            (
                f"  绝密不进公开槽：{'✓' if self.secret_public_ok else '✗'} "
                f"({len(self.secret_public_errors)} 条)"
            ),
            (
                f"  结构完整性：{'✓' if self.structure_ok else '✗'} "
                f"({len(self.structure_errors)} 条)"
            ),
            f"  内容保全（软）：疑似丢失 {len(self.content_preserve_suspects)} 条",
        ]
        if self.all_errors():
            lines.append("错误清单：")
            for e in self.all_errors():
                lines.append(f"  - {e}")
        if self.content_preserve_suspects:
            lines.append("疑似内容丢失：")
            for s in self.content_preserve_suspects:
                ratio = f" hit={s.hit_ratio:.0%}" if s.hit_ratio is not None else ""
                lines.append(f"  - {s.item_id} → {s.dest_kind}/{s.dest_id}:{ratio} {s.reason}")
        return "\n".join(lines)


def normalize_skill_name(skill_name: str) -> str:
    """机械别名归一（组装层常见写法 → 规则表名）。"""
    s = skill_name.strip()
    if not s:
        return s
    if s in SKILL_ALIASES:
        return SKILL_ALIASES[s]
    # 去「检定」后缀
    if s.endswith("检定"):
        base = s[: -len("检定")].strip()
        if base in SKILL_ALIASES:
            return SKILL_ALIASES[base]
        s = base
    return s.replace("侦查", "侦察")


def skill_resolvable(skill_name: str, ruleset: RulesetRead) -> bool:
    """与 app.core.keeper.tools._resolve_skill_target 同源口径（不依赖角色卡）。

    支持：技能中文名 / 技能 id / 英文名 / 属性中文名或缩写；
    另接受组装层常见别名（见 SKILL_ALIASES）。
    """
    wanted = normalize_skill_name(skill_name)
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


def normalize_module_skills(raw: dict[str, Any]) -> int:
    """就地归一 nodes[].checks[].skill，返回改动条数。"""
    changed = 0
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        return 0

    def _fix_node(node: dict[str, Any]) -> None:
        nonlocal changed
        checks = node.get("checks")
        if not isinstance(checks, list):
            return
        for check in checks:
            if not isinstance(check, dict):
                continue
            old = str(check.get("skill") or "")
            new = normalize_skill_name(old)
            if new != old:
                check["skill"] = new
                changed += 1
        sub = node.get("sub_node")
        if isinstance(sub, dict):
            _fix_node(sub)

    for n in nodes:
        if isinstance(n, dict):
            _fix_node(n)
    return changed


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


def _dest_kind_of(info: Any) -> str:
    if isinstance(info, dict):
        return str(info.get("dest_kind") or "").strip()
    return str(info or "").strip()


def _dest_id_of(info: Any) -> str:
    if isinstance(info, dict):
        return str(info.get("dest_id") or "").strip()
    return ""


def check_thin_public_slots(assignment_map: dict[str, Any]) -> list[str]:
    """player_intro / opening / meta 各 ≤1，合计 ≤3。"""
    counts: dict[str, list[str]] = {k: [] for k in _THIN_PUBLIC_KINDS}
    for iid, info in assignment_map.items():
        kind = _dest_kind_of(info)
        if kind in counts:
            counts[kind].append(iid)

    errors: list[str] = []
    total = 0
    for kind in _THIN_PUBLIC_KINDS:
        ids = counts[kind]
        total += len(ids)
        if len(ids) > 1:
            errors.append(
                f"薄公开槽 {kind} 归入 {len(ids)} 个片段（上限 1）：{', '.join(sorted(ids))}"
            )
    if total > 3:
        errors.append(f"薄公开槽 player_intro+opening+meta 合计 {total} 个片段（上限 3）")
    return errors


def _audience_is_keeper_secret(audience: str) -> bool:
    """audience 含「绝密 / 守密人 / KP」即视为不得进玩家公开槽。"""
    a = (audience or "").strip()
    if not a:
        return False
    return any(sig in a for sig in ("绝密", "守密人", "KP", "kp"))


def check_secret_not_public(
    assignment_map: dict[str, Any],
    items_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    """绝密/守密人/KP audience 不得归到 player_intro / opening。"""
    errors: list[str] = []
    for iid, info in sorted(assignment_map.items()):
        kind = _dest_kind_of(info)
        if kind not in _PUBLIC_SLOT_KINDS:
            continue
        it = items_by_id.get(iid) or {}
        audience = str(it.get("audience") or "")
        if _audience_is_keeper_secret(audience):
            errors.append(f"片段 {iid!r} audience={audience!r} 却归到公开槽 {kind}")
    return errors


def _kind_has_signal(what_kind: str, signals: tuple[str, ...]) -> bool:
    wk = what_kind or ""
    return any(sig in wk for sig in signals)


def check_structure_integrity(
    items: list[dict[str, Any]],
    module: ScenarioModule | None,
    *,
    raw: dict[str, Any] | None = None,
) -> list[str]:
    """完整性检查：带结局/议程信号的片段存在时，对应数组不得为空。

    路由仍由阶段1 LLM 做；此处只机械核对「信号片段没被全丢掉」。
    信号取自片段自己的 what_kind_of_thing，不预设专属路由表。
    """
    ending_signal_ids = [
        str(it.get("id") or "")
        for it in items
        if it.get("id")
        and _kind_has_signal(str(it.get("what_kind_of_thing") or ""), _ENDING_KIND_SIGNALS)
    ]
    agenda_signal_ids = [
        str(it.get("id") or "")
        for it in items
        if it.get("id")
        and _kind_has_signal(str(it.get("what_kind_of_thing") or ""), _AGENDA_KIND_SIGNALS)
    ]

    if module is not None:
        endings_n = len(module.endings)
        agenda_n = len(module.agenda)
    else:
        raw = raw or {}
        endings_n = len(raw.get("endings") or []) if isinstance(raw.get("endings"), list) else 0
        agenda_n = len(raw.get("agenda") or []) if isinstance(raw.get("agenda"), list) else 0

    errors: list[str] = []
    if ending_signal_ids and endings_n == 0:
        errors.append(
            f"存在 what_kind 含结局/结尾/结束 信号的片段 {ending_signal_ids}，"
            f"但 endings[] 为空——结构完整性失败"
        )
    if agenda_signal_ids and agenda_n == 0:
        errors.append(
            f"存在 what_kind 含当前事件/行动规律/时间压力/今晚/期限 信号的片段 "
            f"{agenda_signal_ids}，但 agenda[] 为空——结构完整性失败"
        )
    return errors


def _summary_keywords(summary: str) -> list[str]:
    """从 summary 切宽松关键词，用于内容保全子串匹配。"""
    s = (summary or "").strip()
    if not s:
        return []
    parts = [p for p in _SPLIT_RE.split(s) if len(p) >= _PRESERVE_MIN_KW_LEN]
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _module_text_bucket(
    module: ScenarioModule | None,
    raw: dict[str, Any],
    dest_kind: str,
    dest_id: str,
) -> str | None:
    """按归宿拼出应承载内容的文本；无法定位时返回 None。"""
    if module is not None:
        if dest_kind == "player_intro":
            return module.player_intro or ""
        if dest_kind == "opening":
            if module.opening is None:
                return ""
            parts = [
                module.opening.scene or "",
                module.opening.script or "",
                module.opening.kp_notes or "",
            ]
            return "\n".join(parts)
        if dest_kind == "meta":
            m = module.meta
            return " ".join(
                str(x)
                for x in (
                    m.id,
                    m.title,
                    m.era,
                    m.tone,
                    m.designed_players,
                    m.multi_player_note,
                )
                if x
            )
        if dest_kind == "kp_truth":
            return (
                (module.kp_truth.summary or "") + "\n" + "\n".join(module.kp_truth.key_facts or [])
            )
        if dest_kind == "kp_guidance":
            return "\n".join(f"{k}:{v}" for k, v in (module.kp_guidance or {}).items())
        if dest_kind == "node":
            for n in _iter_nodes(module.nodes):
                if n.id == dest_id:
                    checks = " ".join(
                        f"{c.skill} {c.on_success or ''} {c.on_failure or ''}" for c in n.checks
                    )
                    return f"{n.title}\n{n.kp_text}\n{checks}"
            return None
        if dest_kind == "npc":
            for n in module.npcs:
                if n.id == dest_id:
                    return f"{n.name}\n{n.role or ''}\n{n.kp_notes or ''}\n{n.stats or {}}"
            return None
        if dest_kind == "ending":
            for e in module.endings:
                if e.id == dest_id:
                    return f"{e.title}\n{e.condition or ''}\n{e.trigger or ''}\n{e.text}"
            # dest_id 可能与 ending id 不完全一致：拼全部 endings 做宽松兜底
            if module.endings:
                return "\n".join(
                    f"{e.title}\n{e.condition or ''}\n{e.trigger or ''}\n{e.text}"
                    for e in module.endings
                )
            return None
        if dest_kind == "agenda":
            for a in module.agenda:
                if a.id == dest_id:
                    return f"{a.title or ''}\n{a.trigger}\n{a.kp_text}\n{a.effects or []}"
            if module.agenda:
                return "\n".join(
                    f"{a.title or ''}\n{a.trigger}\n{a.kp_text}" for a in module.agenda
                )
            return None
        if dest_kind == "unassigned":
            return None
        return None

    # schema 失败时从 raw dict 尽力取
    if dest_kind == "player_intro":
        return str(raw.get("player_intro") or "")
    if dest_kind == "kp_truth":
        kt = raw.get("kp_truth") or {}
        if isinstance(kt, dict):
            return (
                str(kt.get("summary") or "")
                + "\n"
                + "\n".join(str(x) for x in (kt.get("key_facts") or []))
            )
    return None


def check_content_preservation(
    items: list[dict[str, Any]],
    assignment_map: dict[str, Any],
    module: ScenarioModule | None,
    raw: dict[str, Any],
) -> list[ContentPreserveItem]:
    """内容保全软校验：归宿非空 +（对单片段/薄字段）summary 关键词宽松命中。

    不硬失败；返回疑似丢失清单。
    目的是抓「整段蒸发」（尤其薄字段多塞），不是逐字比对 LLM 改写。
    """
    suspects: list[ContentPreserveItem] = []
    thin_counts: dict[str, int] = {}
    # dest_id → 归入片段数（多片段实体只做「非空」检查，不做关键词）
    dest_item_counts: dict[tuple[str, str], int] = {}
    for info in assignment_map.values():
        k = _dest_kind_of(info)
        did = _dest_id_of(info)
        if k in _THIN_PUBLIC_KINDS:
            thin_counts[k] = thin_counts.get(k, 0) + 1
        dest_item_counts[(k, did)] = dest_item_counts.get((k, did), 0) + 1

    for it in items:
        iid = str(it.get("id") or "").strip()
        if not iid:
            continue
        info = assignment_map.get(iid)
        if info is None:
            suspects.append(
                ContentPreserveItem(
                    item_id=iid,
                    dest_kind="",
                    dest_id="",
                    reason="未出现在归组映射（孤儿）",
                )
            )
            continue

        dest_kind = _dest_kind_of(info)
        dest_id = _dest_id_of(info)
        if dest_kind == "unassigned":
            suspects.append(
                ContentPreserveItem(
                    item_id=iid,
                    dest_kind=dest_kind,
                    dest_id=dest_id,
                    reason="dest_kind=unassigned，内容无落点",
                )
            )
            continue

        # 薄字段被多片段共享 → 必然装不下（缺陷 B 主信号）
        if dest_kind in _THIN_PUBLIC_KINDS and thin_counts.get(dest_kind, 0) > 1:
            suspects.append(
                ContentPreserveItem(
                    item_id=iid,
                    dest_kind=dest_kind,
                    dest_id=dest_id,
                    reason=f"薄字段 {dest_kind} 被 {thin_counts[dest_kind]} 个片段共享，内容易蒸发",
                )
            )
            continue

        text = _module_text_bucket(module, raw, dest_kind, dest_id)
        if text is None:
            suspects.append(
                ContentPreserveItem(
                    item_id=iid,
                    dest_kind=dest_kind,
                    dest_id=dest_id,
                    reason=f"归宿 {dest_kind}/{dest_id} 在最终模组中找不到对应实体",
                )
            )
            continue
        if not str(text).strip():
            suspects.append(
                ContentPreserveItem(
                    item_id=iid,
                    dest_kind=dest_kind,
                    dest_id=dest_id,
                    reason=f"归宿字段 {dest_kind}/{dest_id} 为空",
                )
            )
            continue

        # 多片段并进同一实体：LLM 改写噪声大，只验非空（上面已过）
        if dest_item_counts.get((dest_kind, dest_id), 0) > 1:
            continue

        # 单片段实体 / 薄字段：做宽松关键词命中
        kws = _summary_keywords(str(it.get("summary") or ""))
        if not kws:
            continue
        hits = sum(1 for kw in kws if kw in text)
        ratio = hits / len(kws)
        if ratio < _PRESERVE_HIT_RATIO:
            suspects.append(
                ContentPreserveItem(
                    item_id=iid,
                    dest_kind=dest_kind,
                    dest_id=dest_id,
                    reason=f"summary 关键词命中率 {ratio:.0%}<{_PRESERVE_HIT_RATIO:.0%}",
                    hit_ratio=round(ratio, 3),
                    keywords_total=len(kws),
                    keywords_hit=hits,
                )
            )
    return suspects


def validate_assembled(
    raw: dict[str, Any],
    *,
    source_item_ids: set[str],
    assignment_map: dict[str, Any],
    items: list[dict[str, Any]] | None = None,
    ruleset: RulesetRead | None = None,
) -> ValidationReport:
    """跑完全部硬校验 + 内容保全软项。

    items：源片段列表（含 what_kind_of_thing / audience / summary）。
    缺省时结构完整性/绝密公开槽/内容保全会尽量降级（无信号则跳过）。
    """
    ruleset = ruleset or build_coc7_ruleset()
    items = items or []
    items_by_id = {str(it["id"]): it for it in items if it.get("id")}

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
        schema_errors = schema_errors or ["ScenarioModule.model_validate 失败"]

    orphan_errors = check_orphans(source_item_ids, assignment_map)
    thin_slot_errors = check_thin_public_slots(assignment_map)
    secret_public_errors = (
        check_secret_not_public(assignment_map, items_by_id) if items_by_id else []
    )
    structure_errors = check_structure_integrity(items, module, raw=raw) if items else []
    suspects = check_content_preservation(items, assignment_map, module, raw) if items else []

    ref_ok = not ref_errors
    skill_ok = not skill_errors
    orphan_ok = not orphan_errors
    leak_ok = not leak_errors
    thin_slot_ok = not thin_slot_errors
    secret_public_ok = not secret_public_errors
    structure_ok = not structure_errors
    ok = (
        schema_ok
        and ref_ok
        and skill_ok
        and orphan_ok
        and leak_ok
        and thin_slot_ok
        and secret_public_ok
        and structure_ok
    )
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
        thin_slot_ok=thin_slot_ok,
        thin_slot_errors=thin_slot_errors,
        secret_public_ok=secret_public_ok,
        secret_public_errors=secret_public_errors,
        structure_ok=structure_ok,
        structure_errors=structure_errors,
        content_preserve_ok=len(suspects) == 0,
        content_preserve_suspects=suspects,
    )
