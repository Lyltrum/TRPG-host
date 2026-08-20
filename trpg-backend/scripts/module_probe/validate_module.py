"""组装产物的机械校验（预处理管线校验闭环）。

与 docs/keeper-design/exec/05 第 3 节 + 06 第 2–3 节 + 07（4b）对齐：

原五项（硬失败）：
1. schema 合法 —— ScenarioModule.model_validate
2. 引用闭合 —— leads_to / exits / contains / sub_node / sub_nodes / same_as /
   visibility_pairs 引用、id 唯一
3. 技能名可查 —— check.skill 能在 COC7 规则表解析（与 keeper tools 同源口径）
4. 无孤儿片段 —— 阶段 1 归组映射覆盖全部源片段 id
5. 不泄密 —— kp_truth.key_facts 关键词在 player_intro + opening.script 零出现
   （只查这两个玩家可见字段；node.kp_text 等本就是 KP 专用，不查）

06 新增三项（硬失败）：
6. 薄公开槽上限 —— player_intro / opening / meta 各 ≤1 片段，合计 ≤3
7. 绝密不进公开槽 —— audience 含绝密/守密人/KP 的片段不得归到 player_intro/opening
8. 结构完整性 —— 源片段 what_kind 带结局/议程信号时，那个片段不得被吞进 kp_truth
   （完整性检查，不是路由逻辑；信号取自片段自身 what_kind_of_thing）

06 内容保全（软，不硬失败）：
- 归宿字段非空 + summary 关键词在对应实体文本中的命中率
- 低于阈值的片段列入「疑似内容丢失」清单，写入报告供人看

脚本只吃结构化数据与映射，不内嵌任何模组正文。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.core.coc7.content import build_coc7_ruleset
from app.core.keeper.contract.module_loader import ModuleNode, ScenarioModule
from app.core.keeper.primitives.skills import skill_id_catalog
from app.dto.game import RulesetRead

# 不泄密：从 key_facts 切出的关键词最短长度。太短（如「先生」）会误伤。
_MIN_KEYWORD_LEN = 4

# 薄公开槽：阶段1 归宿 dest_kind
_THIN_PUBLIC_KINDS = ("player_intro", "opening", "meta")

#: 🔴 **本版本用不上、但必须有名字的归宿。**
#:
#: 预设调查员卡（pregen）是模组常见的附件，而本系统的角色是玩家自己建的，
#: schema 里没有它的位置。此前它在归组时无处可去，模型就近塞进了 `player_intro`
#: ——五张角色卡挤进一个"最多 1 个片段"的薄槽，`thin_slot` 当场拒绝整份模组
#: （复足实测）。模型的理由「预制角色，玩家可见」本身没错，**错的是我们没给它
#: 一个落点**：schema 表达不了的东西会从别处漏出去。
#:
#: 所以给它一个显式的名字，然后**显式降级**——不进 structured，但清点并告知，
#: 跟图片占位是同一条纪律（禁止静默丢弃）。
#:
#: `front_matter` 是同一个病的第二例（真机实测）：目录、版权页、译者说明、页码、
#: 出版方 logo——它们**看起来最像 `meta`**，于是八个片段一起挤进那个"最多 1 个"
#: 的薄槽，`thin_slot` 拒绝整份模组，而回灌归组也修不好（它还是没别的地方可放）。
#:
#: 🔴 **这两个名字都要定义得窄。** 宽了就变成第二个 `kp_truth` 兜底垃圾桶——
#: 那正是阶段 1 硬约束 A 在防的事。判据：**它是不是印在书上但跟"玩这个模组"
#: 无关的东西**；只要还能想出一个玩家或守秘人会用到它的场合，就不属于这里。
OUT_OF_SCOPE_KINDS = ("pregen", "front_matter")
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
    "劝说": "说服",
    "说服检定": "说服",
    "交涉": "话术",
    # ── exec/17 (A) 补齐：真人实测在 5 个模组里实际出现的写法 ──
    "闪躲": "闪避",
    "观察": "侦察",
    "信用": "信用评级",
    "攀登": "攀爬",
    "机械修理": "机械维修",
    "机器维修": "机械维修",
    "重机械操作": "操作重型机械",
    "开锁": "锁匠",
    "操纵（船只）": "驾驶：船舶",
    "操纵：船只": "驾驶：船舶",
    "操纵(船只)": "驾驶：船舶",
    "语言:英语": "外语①",
    "语言：英语": "外语①",
    "艺术（唱歌）": "艺术与手艺①",
    "艺术(唱歌)": "艺术与手艺①",
}

#: COC6 遗留的"技能"，在 COC7 里是**属性检定**（Idea→INT×5、Know→EDU×5）。
#: 5 个模组里共 11 条，是最大的一类脏数据——同义词表救不了它们，因为
#: COC7 规则表里压根没有这两个技能。
COC6_ATTRIBUTE_CHECKS: dict[str, str] = {
    "灵感": "INT",
    "知识": "EDU",
    "力量(STR)": "STR",
    "力量（STR）": "STR",
}

#: 多选检定点：任一命中即可（模组原文用 `/` 罗列，括号里是 KP 说明）。
#: 组合属性（STR+DEX）同样落在这里——"力量或敏捷任一"是它在本系统里
#: 唯一说得通的表达，不值得为 1 条数据单开一类 schema。
MULTI_SKILL_CHECKS: dict[str, list[str]] = {
    "话术/魅惑(任一交涉技能)": ["fast-talk", "charm", "persuade"],
    "话术/魅惑(任一交涉)": ["fast-talk", "charm", "persuade"],
    "话术/魅惑/信用(贿赂,需先弄到酒)": ["fast-talk", "charm", "credit-rating"],
    "STR+DEX": ["STR", "DEX"],
}

#: 理智检定：模组把它写进了 checks[]，但它该走 san_checks。
SAN_CHECK_WRITINGS: frozenset[str] = frozenset({"理智", "理智(San)", "理智（San）", "San", "SAN"})


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
    # P1（exec/15）：事实表闭合性与可达性
    facts_ok: bool = True
    facts_errors: list[str] = field(default_factory=list)
    # 06 新增硬项
    thin_slot_ok: bool = True
    thin_slot_errors: list[str] = field(default_factory=list)
    secret_public_ok: bool = True
    secret_public_errors: list[str] = field(default_factory=list)
    structure_ok: bool = True
    structure_errors: list[str] = field(default_factory=list)
    # exec/29 §4 溯源：**只有「完全没有锚点」是硬的**，逐字重合太低已降为软项
    # （见 `check_source_traceability` 的两头标定）。
    trace_ok: bool = True
    trace_errors: list[str] = field(default_factory=list)
    trace_suspects: list[str] = field(default_factory=list)  # 仅作观察；不参与 ok
    # exec/29 §4.6 ⑥：骰型/百分比必须在原文出现过
    numeric_ok: bool = True
    numeric_errors: list[str] = field(default_factory=list)
    # exec/30 §9：自称 encounter 的节点必须在图里走得到
    reach_ok: bool = True
    reach_errors: list[str] = field(default_factory=list)
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
        out.extend(f"[facts] {e}" for e in self.facts_errors)
        out.extend(f"[thin_slot] {e}" for e in self.thin_slot_errors)
        out.extend(f"[secret_public] {e}" for e in self.secret_public_errors)
        out.extend(f"[structure] {e}" for e in self.structure_errors)
        out.extend(f"[trace] {e}" for e in self.trace_errors)
        out.extend(f"[numeric] {e}" for e in self.numeric_errors)
        out.extend(f"[reach] {e}" for e in self.reach_errors)
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
            "facts": {"ok": self.facts_ok, "errors": self.facts_errors},
            "thin_slot": {"ok": self.thin_slot_ok, "errors": self.thin_slot_errors},
            "secret_public": {
                "ok": self.secret_public_ok,
                "errors": self.secret_public_errors,
            },
            "structure": {"ok": self.structure_ok, "errors": self.structure_errors},
            "trace": {
                "ok": self.trace_ok,
                "errors": self.trace_errors,
                "suspects": self.trace_suspects,
            },
            "numeric": {"ok": self.numeric_ok, "errors": self.numeric_errors},
            "reach": {"ok": self.reach_ok, "errors": self.reach_errors},
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
            f"  事实表闭合：{'✓' if self.facts_ok else '✗'} ({len(self.facts_errors)} 条)",
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
            (
                f"  溯源锚点：{'✓' if self.trace_ok else '✗'} ({len(self.trace_errors)} 条)"
                f"；逐字重合偏低（软）{len(self.trace_suspects)} 条"
            ),
            f"  数值忠实度：{'✓' if self.numeric_ok else '✗'} ({len(self.numeric_errors)} 条)",
            f"  遭遇可达：{'✓' if self.reach_ok else '✗'} ({len(self.reach_errors)} 条)",
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
    """与运行时执行层同源口径（不依赖角色卡）。

    运行时那一侧是
    `app.core.keeper.capabilities.skill_check.executor.resolve_skill_target`。

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


# 实体文本与其源行的最低词面关联（`exec/29 §4`）。
#
# 🔴 **这道门抓不到「改写」与「编造」的边界，别指望它。** 在林中屋（唯一未经
# 人手修改的组装产物）上实测，重合值是一条 3→11→18→30+ 的**连续分布，没有自然
# 断点**——阈值取 6 抓 2 个、取 8 抓 3 个、取 12 抓 7 个，全是任意的。根因是
# 组装阶段本来就在改写，改写与编造在词面上没有分界。
#
# 所以它只当**兜底**：中文里 3 字约等于一个词，`< 3` 意味着**连一个词都没对上**，
# 那已经不是改写，是这段文本跟它声称的出处没有关系。真正能抓错位/编造的是
# ③ AI 玩家试跑，不是这里。
MIN_TRACE_RUN = 3


def _compact(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _longest_run_in(needle: str, hay: str) -> int:
    """needle 里能在 hay 中逐字找到的最长连续片段长度。

    与 `access/leak_guard.py` 判泄密同一手法——**逐字重合，不问模型**。
    """
    if not needle or not hay:
        return 0
    best = 0
    n = len(needle)
    for i in range(n):
        if n - i <= best:
            break
        lo, hi = best, n - i
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if needle[i : i + mid] in hay:
                lo = mid
            else:
                hi = mid - 1
        best = max(best, lo)
    return best


def _entity_own_text(obj: dict[str, Any], keys: tuple[str, ...]) -> str:
    return _compact(" ".join(str(obj.get(k) or "") for k in keys))


def _walk_entities(module: dict[str, Any]) -> list[tuple[str, str, str, str | None]]:
    """列出全部实体：`(kind, id, 自身文本, 父实体 id)`。

    父 id 只对 `sub_nodes` 非空——子节点是组装阶段现拆的，没有自己的归组映射，
    要靠它向上继承锚点。
    """
    out: list[tuple[str, str, str, str | None]] = []

    def walk_nodes(nodes: Any, parent: str | None) -> None:
        for nd in nodes or []:
            if not isinstance(nd, dict):
                continue
            nid = str(nd.get("id") or "")
            text = _entity_own_text(nd, ("title", "kp_text", "public_text"))
            for chk in nd.get("checks") or []:
                if isinstance(chk, dict):
                    text += _entity_own_text(chk, ("on_success", "on_failure", "on_fumble"))
            out.append(("node", nid, text, parent))
            walk_nodes(nd.get("sub_nodes"), nid)

    walk_nodes(module.get("nodes"), None)
    for npc in module.get("npcs") or []:
        if isinstance(npc, dict):
            out.append(
                (
                    "npc",
                    str(npc.get("id") or ""),
                    _entity_own_text(npc, ("name", "kp_notes", "public_text")),
                    None,
                )
            )
    for end in module.get("endings") or []:
        if isinstance(end, dict):
            out.append(
                ("ending", str(end.get("id") or ""), _entity_own_text(end, ("title", "text")), None)
            )
    for ag in module.get("agenda") or []:
        if isinstance(ag, dict):
            out.append(
                (
                    "agenda",
                    str(ag.get("id") or ""),
                    _entity_own_text(ag, ("title", "kp_text")),
                    None,
                )
            )
    return out


def build_entity_anchors(
    items: list[dict[str, Any]],
    assignment_map: dict[str, Any],
    module: dict[str, Any],
    source_lines: list[str],
) -> dict[str, str]:
    """实体 id → 它认领的那几行原文（压紧空白）。子节点向上继承祖先锚点。

    抽出来是因为**自修也要用**：`[numeric]`/`[trace]` 这两类错误说的是「跟原文
    对不上」，而自修器手里只有模组 JSON —— 不把相关原文一起给它，它只能靠猜，
    而猜正是这两道门要禁的。
    """
    by_id = {str(it.get("id")): it for it in items if it.get("id")}
    direct: dict[str, str] = {}
    for iid, info in assignment_map.items():
        it = by_id.get(str(iid))
        if it is None:
            continue
        a, b = it.get("line_start"), it.get("line_end")
        if not isinstance(a, int) or not isinstance(b, int):
            continue
        a, b = max(1, min(a, b)), min(len(source_lines), max(a, b))
        if a > b:
            continue
        did = _dest_id_of(info)
        direct[did] = direct.get(did, "") + _compact("".join(source_lines[a - 1 : b]))

    parents = {eid: parent for _k, eid, _t, parent in _walk_entities(module)}
    resolved: dict[str, str] = {}
    for _kind, eid, _text, _parent in _walk_entities(module):
        hay, cursor = direct.get(eid, ""), eid
        while not hay:
            cursor = parents.get(cursor) or ""
            if not cursor:
                break
            hay = direct.get(cursor, "")
        if hay:
            resolved[eid] = hay
    return resolved


def check_source_traceability(
    items: list[dict[str, Any]],
    assignment_map: dict[str, Any],
    module: dict[str, Any],
    source_lines: list[str],
) -> tuple[list[str], list[str]]:
    """溯源检查。返回 `(硬失败, 软疑点)`——**两者的可靠性差着一个量级**。

    ## 硬：完全没有锚点

    实体连一个归组片段都认领不到（也没有可继承的祖先）。这是个**结构事实**，
    二值、不含相似度判断，误报只可能来自归组映射本身坏掉。它继续阻断产出。

    ## 软：逐字重合低于 `MIN_TRACE_RUN`

    🔴 **这一条曾经是硬门，2026-08-05 两头标定后降级。** 标定方法是判据要求的
    「造一个必然通过和一个必然失败的样本」，而**第一次的失败样本造得太容易了**
    （飞船/甜点/服务器这种外行话），它让这道门看起来能分开。换成**同题材**的
    编造（用模组里真有的词说原文没有的事）再量，78 个真实实体 + 312 组编造：

        门限 3： 真实误拒 2/78      编造漏放 224/312（72%）
        门限 4： 真实误拒 7/78      编造漏放  52/312

    没有可用的门限，换判据也没用——2/3/4-gram 覆盖率同样重叠（真实最低 0.10 对
    编造最高 0.29）。**因为这本来就是语义判断**：「照原文压缩」和「照着腔调编」
    在词面上没有分界，确定性代码分不了（CLAUDE.md：别把语义任务交给确定性代码）。

    而它的代价是实打实的：一个实体被误判，**整份模组被拒**。两次误拒都手工核过——
    `唐尼·凯泽` 原文写作 `唐尼.凯泽`（差一个分隔符），老鼠那条是原文
    「那只老鼠会当场死掉…钻回柜子后的老鼠洞里…人是没办法进去的」的忠实压缩。

    所以它留下来当**报出来的信号**，不再有否决权。真能抓编造的是 AI 玩家试跑。
    """
    if not source_lines:
        return [], []

    anchors = build_entity_anchors(items, assignment_map, module, source_lines)

    errors: list[str] = []
    suspects: list[str] = []
    for kind, eid, text, _parent in _walk_entities(module):
        if not eid or not text:
            continue
        hay = anchors.get(eid, "")
        if not hay:
            errors.append(f"{kind} {eid!r} 没有溯源锚点（无归组片段，也没有可继承的祖先）")
            continue
        run = _longest_run_in(text, hay)
        if run < MIN_TRACE_RUN:
            suspects.append(
                f"{kind} {eid!r} 与源行的最长逐字重合仅 {run} 字（下限 {MIN_TRACE_RUN}）"
            )
    return errors, suspects


#: 有语义的数值 token：骰型（1d6 / 4D6+1）与百分比。
#: 🔴 **故意不含孤立整数**——id 序号、年龄、数量词噪声太大，查了全是假阳性。
_NUMERIC_TOKENS = re.compile(r"\d*d\d+(?:[+\-]\d+)?|\d{1,3}%")


def _normalize_numeric(s: str) -> str:
    """归一大小写与全半角。

    🔴 不做这一步会报一堆假阳性：原文写 `1D6`、产物写 `1d6`，实测五份模组里
    有 8 个是这么来的。
    """
    return unicodedata.normalize("NFKC", s or "").lower()


def check_numeric_fidelity(module: dict[str, Any], source_lines: list[str]) -> list[str]:
    """产物里的骰型/百分比必须在原文里出现过（`exec/29 §4.6 ⑥`）。

    数值是模组里**唯一具有清晰对错的东西**：散文改写没有对错，`1d6` 写成 `1d4`
    就是错。而它是唯一能抓到这类错的检查——结构校验说合法、词面兜底说重合充足、
    AI 玩家试跑少扣两点 SAN 照样通关。

    🔴 **判据是全文，不是源行。** 源行级假阳性太高（实测复足 21%）：组装会跨片段
    合并，而锚点是片段粒度。代价是抓不到「数值从别的场景搬过来」——那属于错位，
    本来就归 AI 玩家试跑。

    🔴 **当前几乎零命中**（五份实测 95.7–100% 可定位）。它守的是「以后会不会错」，
    不是「现在错了」。
    """
    if not source_lines:
        return []

    haystack = _normalize_numeric("".join(source_lines))
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for kind, eid, text, _parent in _walk_entities(module):
        for tok in _NUMERIC_TOKENS.findall(_normalize_numeric(text)):
            if tok in haystack or (kind, tok) in seen:
                continue
            seen.add((kind, tok))
            errors.append(f"{kind} {eid!r} 的数值 {tok!r} 在原文里找不到——疑似凭空生成")
    return errors


def _strip_check_suffix(s: str) -> str:
    """剥掉「检定」后缀。

    🔴 判**类别**（SAN / 多技能 / 属性）之前必须先剥：原实现是先查
    `SAN_CHECK_WRITINGS` 再剥后缀，于是「理智检定」这种写法直接漏过去
    （林中屋实测 2/3 条硬失败就是它）。别名表收多少种写法都补不完这个洞——
    洞在顺序上，不在表的大小上。
    """
    s = s.strip()
    return s[: -len("检定")].strip() if s.endswith("检定") else s


def render_skill_whitelist(ruleset: RulesetRead | None = None) -> str:
    """把「合法的检定 id」渲染成给组装模型看的白名单。

    ## 🔴 为什么是白名单，不是更大的别名表

    `resolve_check_skill` 的 docstring 写着「输入是固定的 5 个模组文件，组装期
    不是运行时，所以别名表不算打地鼠」。**模组导入把那个前提拿掉了**——输入
    变成用户随手传的任何一份 PDF，别名表当场退化成打地鼠：真机实测撞到
    `电器维修`（规则表里是「电气维修」，差一个字）、`踢`（该归到格斗：斗殴）、
    `INT×4`（属性倍数检定，规则表里根本没有这个条目）。

    判据早就写好了：**不要用自由文本当标识符，解决它的是 enum**。所以做法不是
    继续往别名表里加词，是**在模型写的时候就让它从这张表里挑**。同一份 id 表
    既喂给模型也喂给 `check_skills`，两边不可能漂。

    属性也在表里：`INT×5`（灵感）这类属性检定在 COC7 里是合法的检定点，
    而它不是技能——不给它一个合法的落点，模型就只能编一个技能名。

    🔴 **表本身取自 `skill_id_catalog`——跟 `check_skills` 是同一个函数**，不是
    另抄一份 `ruleset.skills`。两份拷贝会漂，而漂的症状是「我们发给模型的 id，
    我们自己的校验不认」，模型怎么改都过不去。
    """
    ruleset = ruleset or build_coc7_ruleset()
    catalog = skill_id_catalog(ruleset)
    attr_keys = {a.key for a in ruleset.attributes}
    skills = [f"  {i} {n}" for i, n in catalog.items() if i not in attr_keys]
    attrs = [f"  {i} {n}" for i, n in catalog.items() if i in attr_keys]
    return (
        "【检定 id 白名单——checks[].skill_ids 里的每一项都必须**逐字**取自下表】\n"
        "技能：\n" + "\n".join(skills) + "\n"
        "属性（用于「智力×5」「幸运」这类属性检定，写属性 id 本身）：\n" + "\n".join(attrs)
    )


#: 「属性 × 倍数」这个**写法**：`INT×4`、`智力×5`、`POW x 5`、`EDU*5`。
#: 全角/半角乘号、大小写 x、空格都算。
_ATTR_MULTIPLIER = re.compile(r"^(?P<attr>[^\s×xX*✕]+)\s*[×xX*✕]\s*\d+$")


def attribute_multiplier_check(name: str, ruleset: RulesetRead) -> str | None:
    """把「属性×倍数」解析成属性 id；不是这个形状就返回 None。

    ## 🔴 这是规则，不是同义词表

    真机实测反复撞到 `INT×4`。它不是"智力的另一种叫法"——**是 COC 表达属性检定
    的写法**：COC6 的灵感是 INT×5、知识是 EDU×5，模组里各种倍数都写得出来。
    往别名表里加 `INT×4` 只能挡住这一个数字，下一份模组写 `INT×3` 又漏。

    跟 `specialization_candidates`（专项名 ⊂ 母技能）同族：**结构关系做成规则，
    只有真·同义词才进表。**

    倍数被丢掉是**有意的**：本系统的属性检定难度走 `SUCCESS_TIERS`（÷2 / ÷5），
    没有"×N"这一档。保留它没有落点，而 `灵感 → INT` 早就是这么处理的。
    """
    m = _ATTR_MULTIPLIER.match((name or "").strip())
    if not m:
        return None
    wanted = normalize_skill_name(m.group("attr"))
    for attr in ruleset.attributes:
        # 属性 key 是大写三字母，而模组里 `int×4` 这样写也很常见——
        # 大小写不是同义词问题，直接归一。
        if wanted == attr.label or wanted.upper() == attr.key:
            return attr.key
    return None


def specialization_candidates(name: str, ruleset: RulesetRead) -> list[str]:
    """把「动物学」这种**专项名**匹配回完整技能名（`科学：动物学`）。

    模组写专项名不带母技能很常见。这不是同义词——是「专项 ⊂ 母技能」的结构
    关系，所以做成规则而不是查表。

    🔴 **返回全部候选，由调用方在多于一个时放弃。** 当前规则表 33 个专项名
    零重名、也不与顶层技能撞名，但规则表会变；歧义时猜一个就等于悄悄改了模组。
    """
    wanted = name.strip()
    if not wanted:
        return []
    out: list[str] = []
    for spec in ruleset.skills:
        for sep in ("：", ":"):
            if sep in spec.name and spec.name.split(sep, 1)[1].strip() == wanted:
                out.append(spec.name)
                break
    return out


def resolve_check_skill(raw_skill: str, ruleset: RulesetRead) -> tuple[str, list[str], str]:
    """把模组里的一条检定点写法解析成 `(kind, skill_ids, 展示名)`（exec/17 (A)）。

    解析不出时返回 `("skill", [], 原文)`——**不猜**。调用方（`check_skills`）
    会因为 `skill_ids` 为空而报错并阻断产出，脏数据从此进不了 structured.json。

    这份别名表是**一次性离线转换**的输入，不是运行时的猜测器：输入是固定的
    5 个模组文件，转换完模组里存的就是 id 了。打地鼠之所以是打地鼠，是因为
    运行时面对的输入无穷无尽——组装期不是。
    """
    s = (raw_skill or "").strip()
    if not s:
        return "skill", [], ""
    # 类别判定一律基于剥掉「检定」后缀的写法，见 `_strip_check_suffix`
    base = _strip_check_suffix(s)
    if base in SAN_CHECK_WRITINGS:
        return "san", [], "理智检定"
    if base in MULTI_SKILL_CHECKS:
        ids = MULTI_SKILL_CHECKS[base]
        catalog = skill_id_catalog(ruleset)
        return "skill", ids, "/".join(catalog.get(i, i) for i in ids)
    if base in COC6_ATTRIBUTE_CHECKS:
        key = COC6_ATTRIBUTE_CHECKS[base]
        catalog = skill_id_catalog(ruleset)
        return "skill", [key], catalog.get(key, key)
    attr_key = attribute_multiplier_check(base, ruleset)
    if attr_key is not None:
        catalog = skill_id_catalog(ruleset)
        return "skill", [attr_key], catalog.get(attr_key, attr_key)
    wanted = normalize_skill_name(s)
    for attr in ruleset.attributes:
        if wanted in (attr.key, attr.label):
            return "skill", [attr.key], attr.label
    for spec in ruleset.skills:
        if wanted in (spec.id, spec.name) or (
            spec.name_en is not None and wanted.lower() == spec.name_en.lower()
        ):
            return "skill", [spec.id], spec.name
    # 兜底之前的最后一条**结构性**规则：光写了专项名（「动物学」）。
    # 唯一匹配才接受——歧义时宁可退回原文让 check_skills 报错。
    candidates = specialization_candidates(wanted, ruleset)
    if len(candidates) == 1:
        for spec in ruleset.skills:
            if spec.name == candidates[0]:
                return "skill", [spec.id], spec.name
    return "skill", [], s


def normalize_module_skills(raw: dict[str, Any], ruleset: RulesetRead | None = None) -> int:
    """就地把 nodes[].checks[] 归一成 `(kind, skill_ids, 展示名)`，返回改动条数。

    ## 🔴 已经合法的 `skill_ids` 优先于展示名

    组装期现在把白名单发给模型、让它直接挑 id（`render_skill_whitelist`）。
    此前这里**只从展示名解析、并覆盖 `skill_ids`**——于是模型 id 挑对了、
    中文名写岔一个字（「电器维修」对「电气维修」），正确的 id 会被反过来擦掉，
    然后 `check_skills` 报「未归一」。id 是机器可读的那一份，它说了算；
    展示名从表里回填即可。
    """
    ruleset = ruleset or build_coc7_ruleset()
    catalog = skill_id_catalog(ruleset)
    changed = 0
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        return 0

    def _fix_node(node: dict[str, Any]) -> None:
        nonlocal changed
        checks = node.get("checks")
        if isinstance(checks, list):
            for check in checks:
                if not isinstance(check, dict):
                    continue
                old_name = str(check.get("skill") or "")
                old_ids = list(check.get("skill_ids") or [])
                old_kind = check.get("kind") or "skill"
                if old_kind != "san" and old_ids and all(sid in catalog for sid in old_ids):
                    display = "/".join(catalog[sid] for sid in old_ids)
                    if display != old_name:
                        check["skill"] = display
                        changed += 1
                    continue
                kind, ids, display = resolve_check_skill(old_name, ruleset)
                if (ids, kind, display) != (old_ids, old_kind, old_name):
                    check["skill_ids"] = ids
                    check["kind"] = kind
                    check["skill"] = display
                    changed += 1
        sub = node.get("sub_node")
        if isinstance(sub, dict):
            _fix_node(sub)
        subs = node.get("sub_nodes")
        if isinstance(subs, list):
            for child in subs:
                if isinstance(child, dict):
                    _fix_node(child)

    for n in nodes:
        if isinstance(n, dict):
            _fix_node(n)
    return changed


def drop_unresolvable_checks(raw: dict[str, Any]) -> list[str]:
    """扔掉归一之后仍然没有 `skill_ids` 的检定点。返回扔掉了哪几条。

    ## 🔴 为什么是"扔掉"而不是"报错"

    真机实测（坨子岛）：`node 'glow-heartbeat' checks[2] 未归一到技能 id
    （原文 ''）`——**模型吐了一个空的技能名**。这条 check 里没有任何可用信息：
    没有技能、没有 id，什么都判不了。

    留着它 = 整份模组作废；扔掉它 = 少一个检定点，那一幕照样能主持。用户对这条
    线的定位是「**只能成功不能失败**，遇到失败先找更窄的修法」——一个空 check
    正是最该被窄化掉的东西。

    ⚠️ 只扔 `kind != "san"` 且 `skill_ids` 为空的。理智检定本来就不指向技能
    （`check_skills` 自己也跳过它们），扔了会真的丢东西。

    调用点在 `normalize_module_skills` **之后**——先尽力归一，实在归不出来的
    才扔。顺序反了会把「电器维修」这种一字之差的也当成无解。
    """
    dropped: list[str] = []

    def _walk(node: dict[str, Any], path: str) -> None:
        checks = node.get("checks")
        if isinstance(checks, list):
            kept = []
            for i, check in enumerate(checks):
                if not isinstance(check, dict):
                    continue
                if str(check.get("kind") or "skill") == "san" or check.get("skill_ids"):
                    kept.append(check)
                    continue
                dropped.append(f"{path} checks[{i}]（原文 {str(check.get('skill') or '')!r}）")
            if len(kept) != len(checks):
                node["checks"] = kept
        sub = node.get("sub_node")
        if isinstance(sub, dict):
            _walk(sub, f"{path}/{sub.get('id')}")
        for child in node.get("sub_nodes") or []:
            if isinstance(child, dict):
                _walk(child, f"{path}/{child.get('id')}")

    for n in raw.get("nodes") or []:
        if isinstance(n, dict):
            _walk(n, f"node {str(n.get('id'))!r}")
    return dropped


def normalize_and_prune_checks(raw: dict[str, Any], ruleset: RulesetRead | None = None) -> int:
    """先尽力归一，归不出来的扔掉。返回改动条数（含扔掉的）。

    🔴 **两件事绑在一个函数里是有意的**：调用点有四处，分开写就是「逐个列出的
    地方，加一项就漏一项」——迟早有一处只归一不清理，然后一个空 check 让整份
    模组作废。

    顺序不能反：先 `normalize_module_skills`（把「电器维修」这种一字之差救回来），
    再 `drop_unresolvable_checks`（扔真的无解的）。
    """
    changed = normalize_module_skills(raw, ruleset)
    dropped = drop_unresolvable_checks(raw)
    return changed + len(dropped)


def _collect_node_ids(nodes: list[ModuleNode]) -> set[str]:
    ids: set[str] = set()
    for node in _iter_nodes(nodes):
        ids.add(node.id)
    return ids


def _iter_nodes(nodes: list[ModuleNode]) -> list[ModuleNode]:
    out: list[ModuleNode] = []
    for node in nodes:
        out.append(node)
        if node.sub_node is not None:
            out.extend(_iter_nodes([node.sub_node]))
        if node.sub_nodes:
            out.extend(_iter_nodes(node.sub_nodes))
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
    npc_ids = {n.id for n in module.npcs}
    entity_ids = node_ids | npc_ids
    # id 唯一：node / npc / ending / agenda / visibility_pair
    for label, ids in (
        ("node", [n.id for n in _iter_nodes(module.nodes)]),
        ("npc", [n.id for n in module.npcs]),
        ("ending", [e.id for e in module.endings]),
        ("agenda", [a.id for a in module.agenda]),
        ("visibility_pair", [p.id for p in module.visibility_pairs]),
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
        for target in node.exits:
            if target not in node_ids:
                errors.append(f"node {node.id!r} exits 悬空：{target!r}")
        for target in node.contains:
            if target not in node_ids:
                errors.append(f"node {node.id!r} contains 悬空：{target!r}")
        if node.sub_node is not None and node.sub_node.id not in node_ids:
            errors.append(f"node {node.id!r} sub_node.id 未登记：{node.sub_node.id!r}")
        # branches 里的 then 不引用 node id（outcome 是自由文本）；
        # 若 outcome/condition 里看起来像 id 引用，留给内容层，骨架层不扫文本。

    for npc in module.npcs:
        form_ids: set[str] = set()
        for form in npc.forms:
            if form.id in form_ids:
                errors.append(f"npc {npc.id!r} forms id 重复：{form.id!r}")
            form_ids.add(form.id)
        for other in npc.same_as:
            if other not in npc_ids:
                errors.append(f"npc {npc.id!r} same_as 悬空：{other!r}")

    for pair in module.visibility_pairs:
        if pair.public_ref not in entity_ids:
            errors.append(f"visibility_pair {pair.id!r} public_ref 悬空：{pair.public_ref!r}")
        if pair.secret_ref not in entity_ids:
            errors.append(f"visibility_pair {pair.id!r} secret_ref 悬空：{pair.secret_ref!r}")
    return errors


ENCOUNTER_KIND = "encounter"


def check_encounter_reachability(module: ScenarioModule) -> list[str]:
    """自称遭遇的节点必须走得到（`exec/30 §9`）。

    ## 为什么只管 encounter，不管所有节点

    「孤立节点（无进无出）」这道门 §8.5 试过，**在内置 6 份模组里 4 份命中**——
    背景资料条目本来就不是"走得到的地方"，泛化的可达性会判死我们自己的模组。
    实测同一份好产物 23 个顶层节点里 11 个没有入边，全部合法。

    `kind="encounter"` 不一样：它是**组装层自己声明**的一个封闭类别，语义就是
    「玩家会撞上的一幕」。一幕没有任何入边 = 它在对局里永远不会发生。所以这道
    门问的不是"这模组切得细不细"（语义、代码做不了），而是"这幕接上了没有"
    （图不变量、机械可判）。

    ## 🔴 这道门的边界，别拿它当那个 bug 的探测器

    §9 那份真机坏产物**根本没有 encounter 节点**——整幕的材料压进了
    `npcs[].kp_notes`，一个节点都没生成。所以这道门**抓不到那次失败**。
    它守的是修法的后一半：遭遇有了自己的归宿之后，别再退化成"建了节点但
    悬在图外"。**「材料该不该成为节点」仍然是语义判断，没有机械判据**
    （§8.5 三个候选 + §9 复核时又量废四个，全都在内置模组上误伤）。
    """
    node_ids = _collect_node_ids(module.nodes)
    incoming: dict[str, list[str]] = {}
    for node in _iter_nodes(module.nodes):
        for field_name in ("leads_to", "exits", "contains"):
            for target in getattr(node, field_name):
                if target in node_ids:
                    incoming.setdefault(target, []).append(f"{node.id}.{field_name}")
        if node.sub_node is not None:
            incoming.setdefault(node.sub_node.id, []).append(f"{node.id}.sub_node")
        for sub in node.sub_nodes:
            incoming.setdefault(sub.id, []).append(f"{node.id}.sub_nodes")

    errors: list[str] = []
    for node in _iter_nodes(module.nodes):
        if (node.kind or "").strip().lower() != ENCOUNTER_KIND:
            continue
        if incoming.get(node.id):
            continue
        errors.append(
            f"遭遇节点 {node.id!r} 没有任何入边（leads_to/exits/contains 都没人指向它），"
            "这一幕在对局里永远不会发生：请从触发它的调查点用 leads_to 指过来，"
            "或用 exits 接上它发生的地点"
        )
    return errors


_FACT_TIERS = {"diegetic", "meta"}
_FACT_KINDS = {"clue", "npc_knowledge", "truth"}


def check_facts(module: ScenarioModule) -> list[str]:
    """事实表的闭合性与可达性（exec/15，P1）。

    facts 为空时全部通过——尚未迁移的模组照常可主持（向后兼容是硬要求）。
    """
    errors: list[str] = []
    if not module.facts:
        return errors

    fact_ids: set[str] = set()
    for fact in module.facts:
        if fact.id in fact_ids:
            errors.append(f"fact id 重复：{fact.id!r}")
        fact_ids.add(fact.id)
        if fact.tier not in _FACT_TIERS:
            errors.append(f"fact {fact.id!r} tier 非法：{fact.tier!r}")
        if fact.kind not in _FACT_KINDS:
            errors.append(f"fact {fact.id!r} kind 非法：{fact.kind!r}")
        if not fact.text.strip():
            errors.append(f"fact {fact.id!r} text 为空")

    # 引用闭合 + 收集揭开路径
    revealed: set[str] = set()
    for node in _iter_nodes(module.nodes):
        for target in node.reveals:
            if target not in fact_ids:
                errors.append(f"node {node.id!r} reveals 悬空：{target!r}")
            else:
                revealed.add(target)
        for i, check in enumerate(node.checks):
            for target in check.reveals:
                if target not in fact_ids:
                    errors.append(f"node {node.id!r} checks[{i}].reveals 悬空：{target!r}")
                else:
                    revealed.add(target)
    for npc in module.npcs:
        for target in npc.knows:
            if target not in fact_ids:
                errors.append(f"npc {npc.id!r} knows 悬空：{target!r}")
            else:
                revealed.add(target)

    by_id = {f.id: f for f in module.facts}
    for fact_id in sorted(revealed):
        fact = by_id[fact_id]
        if fact.tier == "meta":
            # 元层对任何虚构内主体永不可见，不存在"挣得"这回事——被 reveals
            # 引用说明迁移时把主持指导误当成了线索。
            errors.append(f"fact {fact_id!r} 是 meta 层，不该出现在 reveals/knows 里")

    # 死线索：玩家永远拿不到的虚构内事实
    for fact in module.facts:
        if fact.tier == "diegetic" and fact.id not in revealed:
            errors.append(f"fact {fact.id!r} 没有任何揭开路径（死线索）")

    return errors


def check_skills(module: ScenarioModule, ruleset: RulesetRead | None = None) -> list[str]:
    """检定点必须已经归一成白名单 id（exec/17 (A)）。

    这条校验进 `ok`，也就是**阻断产出**：在此之前它只报错不拦，于是 43 条
    脏数据照样能产出可主持的 structured.json，运行时再靠字符串匹配去猜，
    猜不中就静默丢检定。
    """
    ruleset = ruleset or build_coc7_ruleset()
    catalog = skill_id_catalog(ruleset)
    errors: list[str] = []
    for node in _iter_nodes(module.nodes):
        for i, check in enumerate(node.checks):
            if check.kind == "san":
                continue  # 理智检定不指向技能，走 san_checks
            if not check.skill_ids:
                errors.append(
                    f"node {node.id!r} checks[{i}] 未归一到技能 id（原文 {check.skill!r}）"
                )
                continue
            unknown = [sid for sid in check.skill_ids if sid not in catalog]
            if unknown:
                errors.append(f"node {node.id!r} checks[{i}].skill_ids 不在白名单：{unknown}")
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


def count_out_of_scope(assignment_map: dict[str, Any]) -> dict[str, int]:
    """清点被判为「本版本用不上」的片段，按归宿分类。

    🔴 **要清点，因为静默丢弃和"这份模组本来就没有"看起来一模一样。**
    数字会变成给用户的一句显式降级说明，跟图片占位同一条纪律。
    """
    counts: dict[str, int] = {}
    for info in assignment_map.values():
        kind = _dest_kind_of(info)
        if kind in OUT_OF_SCOPE_KINDS:
            counts[kind] = counts.get(kind, 0) + 1
    return counts


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


def _audience_is_keeper_secret(item: dict[str, Any]) -> bool:
    """这个片段能不能进玩家公开槽。

    ## 🔴 2026-08-20：从关键词表换成枚举

    上一版是 `any(sig in a for sig in ("绝密", "守密人", "KP", "kp"))`——拿
    **自由文本**当标识符，而抽取那头是**刻意**不给固定列表的（`probe.py`：
    "按内容如实写，不要强迫二选一"）。五份模组实测 20 多种写法，那张表同时
    有漏判和误判：

    - **漏**：表里写的是「守密**人**」，数据里是「守**秘**人」，一字之差全漏；
    - **误**：`玩家可见（守秘人笔记部分为守秘人绝密）` 含"绝密"，整条被判成
      KP 绝密——而它主体是玩家可见的。

    判据：**不要用自由文本当标识符，要么是白名单 id，要么退化成同义词打地鼠。**
    现在读 `audience_kind`（`assemble.translate_audiences` 产出的枚举）。

    `both` **不算绝密**：它的主体是玩家可见，只是夹着一段主持人笔记——那一段
    该在归组时拆走，不该让整个片段进不了公开槽。

    没有 `audience_kind` 的（老产物、或翻译层没跑）落到 `kp`：判错的代价不对称，
    多藏一段只是主持人多讲一句，剧透一次毁掉整局。
    """
    kind = str(item.get("audience_kind") or "").strip().lower()
    if not kind:
        return True
    return kind == "kp"


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
        if _audience_is_keeper_secret(it):
            audience = str(it.get("audience") or "")
            errors.append(f"片段 {iid!r} audience={audience!r} 却归到公开槽 {kind}")
    return errors


def _kind_has_signal(what_kind: str, signals: tuple[str, ...]) -> bool:
    wk = what_kind or ""
    return any(sig in wk for sig in signals)


def check_structure_integrity(
    items: list[dict[str, Any]],
    assignment_map: dict[str, Any],
) -> list[str]:
    """完整性检查：带结局/议程信号的片段**不得被吞进 kp_truth**。

    ## 🔴 这道门原来问错了问题（2026-08-04 修）

    旧口径是「有结局信号片段 → `endings[]` 不得为空」。它把两件事焊死了：
    「这段材料没丢」和「这段材料落在 endings 里」。而实测林中屋证明第二件
    **不该**成立——原文那一行是「模组尾声，提供战役延续的可能性」，它是收尾
    材料，不是玩家能走到的收束点。旧口径连同阶段1 的 prompt 一起，把它硬顶
    成了 `endings[0]`，于是这个模组永远收束不了，而试跑只会报「没走到结局」。

    这道门真正要防的失败模式，rule C 那行括号里写得很清楚：**不可吞进
    kp_truth**（信息进了守秘人真相块 = 谁也用不上，等于蒸发）。所以现在只查
    这一条：信号片段的归宿不能是 kp_truth。

    **落在哪里仍由阶段1 的 LLM 决定**——`ending` 还是 `kp_guidance` 还是终局
    那个 `node`，是语义判断，机械层不该替它选。门只管兜住那个已知的坏归宿。

    副作用（有意的）：`endings[]` 从此**可以合法为空**。一个开放收尾的模组
    就是没有可到达结局，这是事实，不是缺陷。承认它比伪造一条强——伪造出来的
    那条会一路骗到试跑判据。
    """
    swallowed: list[str] = []
    for it in items:
        iid = str(it.get("id") or "")
        if not iid:
            continue
        wk = str(it.get("what_kind_of_thing") or "")
        if not _kind_has_signal(wk, _ENDING_KIND_SIGNALS + _AGENDA_KIND_SIGNALS):
            continue
        if _dest_kind_of(assignment_map.get(iid)) == "kp_truth":
            swallowed.append(iid)

    if not swallowed:
        return []
    return [f"结局/议程信号片段被吞进 kp_truth（谁也用不上，等于蒸发）：{sorted(swallowed)}"]


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
        if dest_kind in OUT_OF_SCOPE_KINDS:
            # 本来就不进 structured（见 OUT_OF_SCOPE_KINDS），不是"内容丢了"。
            # 它由 `count_out_of_scope` 单独清点并告知用户。
            continue
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
    source_lines: list[str] | None = None,
) -> ValidationReport:
    """跑完全部硬校验 + 内容保全软项。

    items：源片段列表（含 what_kind_of_thing / audience / summary）。
    缺省时结构完整性/绝密公开槽/内容保全会尽量降级（无信号则跳过）。

    source_lines：原文按行切开（`exec/29 §4` 忠实度硬门要它）。
    🔴 **缺省时溯源检查整条跳过**——没有原文就无从比对，此时报告的 `ok`
    不包含忠实度这一层，别拿它当"忠实度过了"。
    """
    ruleset = ruleset or build_coc7_ruleset()
    items = items or []
    items_by_id = {str(it["id"]): it for it in items if it.get("id")}

    module, schema_errors = check_schema(raw)
    schema_ok = module is not None and not schema_errors

    ref_errors: list[str] = []
    skill_errors: list[str] = []
    leak_errors: list[str] = []
    facts_errors: list[str] = []
    reach_errors: list[str] = []
    if module is not None:
        ref_errors = check_refs(module)
        skill_errors = check_skills(module, ruleset)
        leak_errors = check_leak(module)
        facts_errors = check_facts(module)
        reach_errors = check_encounter_reachability(module)
    else:
        schema_errors = schema_errors or ["ScenarioModule.model_validate 失败"]

    orphan_errors = check_orphans(source_item_ids, assignment_map)
    thin_slot_errors = check_thin_public_slots(assignment_map)
    secret_public_errors = (
        check_secret_not_public(assignment_map, items_by_id) if items_by_id else []
    )
    structure_errors = check_structure_integrity(items, assignment_map) if items else []
    trace_errors, trace_suspects = (
        check_source_traceability(items, assignment_map, raw, source_lines)
        if items and source_lines
        else ([], [])
    )
    numeric_errors = check_numeric_fidelity(raw, source_lines) if source_lines else []
    suspects = check_content_preservation(items, assignment_map, module, raw) if items else []

    ref_ok = not ref_errors
    skill_ok = not skill_errors
    orphan_ok = not orphan_errors
    leak_ok = not leak_errors
    facts_ok = not facts_errors
    thin_slot_ok = not thin_slot_errors
    secret_public_ok = not secret_public_errors
    structure_ok = not structure_errors
    trace_ok = not trace_errors
    numeric_ok = not numeric_errors
    reach_ok = not reach_errors
    ok = (
        schema_ok
        and ref_ok
        and skill_ok
        and orphan_ok
        and leak_ok
        and facts_ok
        and thin_slot_ok
        and secret_public_ok
        and structure_ok
        and trace_ok
        and numeric_ok
        and reach_ok
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
        facts_ok=facts_ok,
        facts_errors=facts_errors,
        thin_slot_ok=thin_slot_ok,
        thin_slot_errors=thin_slot_errors,
        secret_public_ok=secret_public_ok,
        secret_public_errors=secret_public_errors,
        structure_ok=structure_ok,
        structure_errors=structure_errors,
        trace_ok=trace_ok,
        trace_errors=trace_errors,
        trace_suspects=trace_suspects,
        numeric_ok=numeric_ok,
        numeric_errors=numeric_errors,
        reach_ok=reach_ok,
        reach_errors=reach_errors,
        content_preserve_ok=len(suspects) == 0,
        content_preserve_suspects=suspects,
    )
