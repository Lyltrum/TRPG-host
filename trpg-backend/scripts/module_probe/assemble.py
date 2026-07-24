"""预处理管线 · 组装层 + 校验闭环（路线 4a/B + 4b 薄骨架）。

把探针产物（裸抽取 + 关系）组装成 ScenarioModule 结构化 JSON。
4b 起目标 schema 含 exits/contains/sub_nodes/forms/visibility_pairs
（见 docs/keeper-design/exec/07）。

阶段（窄合同多次调用，不压成一次干所有事）：
  1. 实体归组 —— 片段 id → 归宿（node/npc/ending/agenda/kp_truth/…）
  2. 实体成形 —— 按归宿合成 node/npc/ending/agenda 对象
  3. 顶层字段 —— meta / kp_truth / player_intro / opening / kp_guidance
  3b. 密级配对 —— visibility_pairs（启发式 + 可选 LLM 补洞）
  然后机械校验（含 4b 引用闭合）+ 内容保全软项；自修 ≤2 次。

用法（在 trpg-backend/ 下）：

    .venv/bin/python scripts/module_probe/assemble.py \\
        --extract ../模组资料/科比特先生.裸抽取.json \\
        --relations-pass1 ../模组资料/科比特先生.关系-pass1.json \\
        --relations-pass2 ../模组资料/科比特先生.关系-pass2.json \\
        --source-txt ../模组资料/科比特先生.txt \\
        --example ../模组资料/追书人.structured.json

产物（默认与 extract 同目录，gitignored）：
  *.structured.json  *.组装中间态.json  *.校验报告.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import OpenAI

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

# 保证能 import app.*
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from probe import (  # noqa: E402
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    load_api_key,
    read_numbered_lines,
)
from validate_module import (  # noqa: E402
    ValidationReport,
    normalize_module_skills,
    validate_assembled,
)

MAX_RETRIES = 3
TEMPERATURE = 0.2
# 自修：校验失败后喂回 LLM 重试的次数上限
MAX_REPAIR = 2

# DeepSeek chat 公开价目（元/百万 token，约值，仅用于汇报估算）
_PRICE_INPUT_PER_M = 1.0
_PRICE_OUTPUT_PER_M = 2.0

# ── 目标 schema 说明（给 LLM，与 module_loader.py 逐字对齐）──────────────
TARGET_SCHEMA_DOC = """\
目标 JSON 形状（ScenarioModule，必须严格对齐）：
{
  "meta": {
    "id": "英文短 id",
    "title": "模组标题",
    "era": "可选",
    "tone": "可选",
    "designed_players": "可选",
    "multi_player_note": "可选"
  },
  "kp_truth": {
    "summary": "KP 绝密真相摘要",
    "key_facts": ["关键事实1", "关键事实2"]
  },
  "player_intro": "唯一直接念给玩家的开场介绍（只能用玩家可见信息）",
  "opening": {
    "scene": "可选场景名",
    "script": "可念给玩家的开场脚本",
    "kp_notes": "可选，KP 备注"
  },
  "agenda": [
    {
      "id": "英文短 id",
      "title": "可选",
      "trigger": "自由文本触发条件（不要枚举）",
      "kp_text": "触发后 KP 应推进的内容",
      "effects": ["可选效果说明"],
      "once": true
    }
  ],
  "nodes": [
    {
      "id": "英文短 id",
      "title": "节点标题",
      "kind": "可选：location|clue|item|event|…",
      "kp_text": "KP 视角完整信息（可含真相）",
      "public_text": "可选，挣得后可念给玩家的摘要",
      "checks": [
        {
          "skill": "中文技能名（COC7 可解析：侦察/聆听/图书馆使用/话术/格斗：斗殴…）",
          "difficulty": "可选，如 普通/困难",
          "on_success": "可选",
          "on_failure": "可选",
          "on_fumble": "可选",
          "prerequisite": "可选"
        }
      ],
      "branches": [
        {"condition": "条件", "outcome": "结果", "then": [{"condition":"…","outcome":"…"}]}
      ],
      "sub_node": null,
      "sub_nodes": [
        {
          "id": "子节点英文短 id",
          "title": "物件或子区域",
          "kind": "item",
          "kp_text": "可寻址细节",
          "checks": [],
          "branches": [],
          "sub_nodes": [],
          "leads_to": [],
          "exits": [],
          "contains": []
        }
      ],
      "exits": ["空间邻接的 node id"],
      "contains": ["被包含的 node/sub 节点 id"],
      "leads_to": ["情节/因果推进的 node id"]
    }
  ],
  "npcs": [
    {
      "id": "英文短 id",
      "name": "显示名",
      "role": "可选",
      "kp_notes": "KP 笔记",
      "public_text": "可选公开摘要",
      "stats": {"STR": 50, "HP": 10},
      "forms": [
        {"id": "form-id", "name": "形态名", "notes": "可选", "stats": {"HP": 12}}
      ],
      "same_as": ["同一实体的其它 npc id"]
    }
  ],
  "endings": [
    {
      "id": "英文短 id",
      "title": "标题",
      "condition": "可选叙述条件",
      "trigger": "可选可判定触发描述",
      "text": "结局文本"
    }
  ],
  "kp_guidance": {"键": "自由文本指引"},
  "visibility_pairs": [
    {
      "id": "pair-id",
      "public_ref": "公开侧 node 或 npc id",
      "secret_ref": "真相侧 node 或 npc id",
      "note": "可选说明"
    }
  ]
}

图边口诀：exits=空间邻接；contains=包含层级；leads_to=情节/因果推进。三者引用的 id 必须存在。
"""


@dataclass
class CallStats:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    stage_logs: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    retries: list[dict[str, Any]] = field(default_factory=list)

    def estimate_cost_cny(self) -> float:
        return (
            self.prompt_tokens / 1_000_000 * _PRICE_INPUT_PER_M
            + self.completion_tokens / 1_000_000 * _PRICE_OUTPUT_PER_M
        )


def _chat_json(
    client: OpenAI,
    *,
    system: str,
    user: str,
    temperature: float,
    stats: CallStats,
    label: str,
) -> dict[str, Any]:
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            t0 = time.perf_counter()
            response = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                temperature=temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            elapsed = time.perf_counter() - t0
            stats.calls += 1
            usage = response.usage
            pt = ct = tt = 0
            if usage is not None:
                pt = usage.prompt_tokens or 0
                ct = usage.completion_tokens or 0
                tt = usage.total_tokens or 0
                stats.prompt_tokens += pt
                stats.completion_tokens += ct
                stats.total_tokens += tt
            raw = response.choices[0].message.content or ""
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError(f"响应不是 JSON 对象：{type(data).__name__}")
            stats.stage_logs.append(
                {
                    "label": label,
                    "elapsed_s": round(elapsed, 2),
                    "attempt": attempt,
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "total_tokens": tt,
                }
            )
            print(
                f"  {label}: ok ({elapsed:.1f}s, attempt {attempt}, tokens in={pt} out={ct})",
                flush=True,
            )
            if attempt > 1:
                stats.retries.append(
                    {
                        "label": label,
                        "attempts": attempt,
                        "reason": str(last_err) if last_err else "retry",
                    }
                )
            return data
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            print(
                f"  {label}: attempt {attempt}/{MAX_RETRIES} failed: {exc}",
                flush=True,
            )
            if attempt < MAX_RETRIES:
                time.sleep(1.5 * attempt)
    stats.failures.append({"label": label, "error": str(last_err)})
    raise RuntimeError(f"{label} 在 {MAX_RETRIES} 次尝试后仍失败: {last_err}")


def _item_body(lines: list[str], item: dict[str, Any]) -> str:
    try:
        a = int(item["line_start"])
        b = int(item["line_end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"条目 {item.get('id')!r} 缺少有效行号") from exc
    if a > b:
        a, b = b, a
    a = max(1, a)
    b = min(len(lines), b)
    return "\n".join(lines[ln - 1] for ln in range(a, b + 1))


def merge_relations(
    pass1: list[dict[str, Any]],
    pass2: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并两遍关系：同一无序对保留两条不同描述，去重完全相同的。"""
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for src in (pass1, pass2):
        for r in src:
            a = str(r.get("item_a") or "").strip()
            b = str(r.get("item_b") or "").strip()
            rel = str(r.get("relation") or "").strip()
            if not a or not b or not rel:
                continue
            key = (min(a, b), max(a, b), rel)
            if key in seen:
                continue
            seen.add(key)
            out.append({"item_a": a, "item_b": b, "relation": rel})
    return out


def format_item_catalog(items: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for it in items:
        lines.append(f"- id: {it.get('id')}")
        lines.append(f"  title: {it.get('title') or ''}")
        lines.append(f"  what_kind_of_thing: {it.get('what_kind_of_thing') or ''}")
        lines.append(f"  audience: {it.get('audience') or ''}")
        lines.append(f"  summary: {it.get('summary') or ''}")
        lines.append("")
    return "\n".join(lines)


def format_relations(rels: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for r in rels:
        lines.append(f"- {r['item_a']} ↔ {r['item_b']}: {r['relation']}")
    return "\n".join(lines)


def load_example_skeleton(path: Path | None) -> str:
    """从格式范例抽出**形状骨架**（字段名+类型提示），不把范例正文塞进产物。

    🔴 范例可能是第三方模组（如追书人）；这里只取结构，绝不把范例内容
    写进科比特产物。
    """
    if path is None or not path.is_file():
        return TARGET_SCHEMA_DOC

    raw = json.loads(path.read_text(encoding="utf-8"))

    def skeleton(obj: Any, depth: int = 0) -> Any:
        if depth > 4:
            return "…"
        if isinstance(obj, dict):
            return {k: skeleton(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, list):
            if not obj:
                return []
            return [skeleton(obj[0], depth + 1)]
        if isinstance(obj, bool):
            return "bool"
        if isinstance(obj, int | float):
            return "number"
        return "string"

    sk = skeleton(raw)
    return (
        TARGET_SCHEMA_DOC
        + "\n\n格式范例的字段骨架（仅形状，内容已抹去，不要抄范例正文）：\n"
        + json.dumps(sk, ensure_ascii=False, indent=2)
    )


# ── 阶段 1：实体归组 ──────────────────────────────────────────

STAGE1_SYSTEM = """\
你是模组预处理流水线的「实体归组」阶段。

输入：一份模组已被切成的全部片段清单 + 片段间关系。
任务：把**每个片段**分配到一个归宿，并产出实体清单。

归宿种类（dest_kind）：
- node —— 调查场景/地点/可到达节点/可发现的线索与行动（会进入 nodes[]）
- npc —— 人物或怪物（会进入 npcs[]）
- ending —— 结局相关（会进入 endings[]）
- agenda —— 不依赖玩家行动、世界自己会推进的事件/时间压力（会进入 agenda[]）
- kp_truth —— 顶层 KP 绝密真相（**仅**纯背景真相/超自然设定）
- kp_guidance —— 顶层 KP 主持指引（节奏、战斗警告、跑偏处理等）
- opening —— 开场脚本素材（薄字段，最多 1 个片段）
- player_intro —— 玩家开场介绍素材（薄字段，最多 1 个片段；仅玩家可见）
- meta —— 模组元信息（薄字段，最多 1 个片段）

规则：
1. **无孤儿**：每个输入片段 id 必须出现在 assignments 里恰好一次。
2. 被聚合/层级/包含类关系连在一起的片段，应归入同一实体（同一 dest_kind + dest_id）。
3. dest_id 用英文小写连字符；同一实体的多片段共享同一个 dest_id。
4. 分不掉的片段 dest_kind 用 "unassigned"，并在 orphans 里说明原因——禁止静默丢弃。
5. 不要发明清单里没有的片段 id。
6. 只输出 JSON，不要 markdown 围栏。

【硬约束——违反即整批作废】
A. **kp_truth 不是兜底垃圾桶**：只装「背景真相 / 超自然设定」这类**纯背景**片段。
   若片段本身就是结局、事件、线索、地点、NPC、数据块、行动描述——归到对应结构位
  （ending / agenda / node / npc …），**不许因为「和真相有关」就丢进 kp_truth**
  （几乎所有片段都和真相有关）。
B. **薄公开字段各只收 1 个片段**：player_intro / opening / meta **各自最多归入 1 个**
   片段——那个真正「公开开场框架 / 元信息」的片段。
   - meta：只放版权/标题/人数这类元信息（通常 1 个 credits 片段）。
   - opening：只放**玩家可见**的开场场景脚本（通常 1 个 initial-scene 类）。
   - player_intro：只放**玩家可见**的开场介绍框架（若 opening 已覆盖开场，player_intro
     可空——即 0 个片段也合法，不要硬塞）。
   - 模组简介若 audience 是守密人/KP → 进 **kp_guidance**，不要塞 opening/meta。
   **调查线索**（报纸剪报、证词、现场发现、可挣得的情报）、**调查行动**、**地点描述**
   一律进 **node**（在某个调查节点里被发现），**禁止**塞进 player_intro。
   player_intro+opening+meta 合计 ≤ 3。
C. **结局与时间压力要建实体**（强制，不可吞进 kp_truth）：
   - what_kind_of_thing 指示「结局 / 结尾 / 结束」→ dest_kind=**ending**，建 ending 实体；
   - 指示「当前事件 / 行动规律 / 时间压力 / 今晚 / 期限」等时间驱动 → dest_kind=**agenda**，
     建 agenda 条目（trigger 用自由文本）。
   - 可有多条 agenda；至少覆盖所有带上述信号的片段。
D. **利用 audience 辅助归组**：
   - audience 含「绝密 / 守密人 / KP」的片段 **不得** 归到 player_intro 或 opening；
   - 玩家可见的调查线索仍应进 node，不是 player_intro。

输出形状：
{
  "entities": [
    {"kind": "node|npc|ending|agenda|…", "id": "…", "title": "…", "item_ids": ["…"]}
  ],
  "assignments": [
    {"item_id": "…", "dest_kind": "…", "dest_id": "…", "reason": "一句话"}
  ],
  "orphans": [{"item_id": "…", "why": "…"}]
}
"""


def stage1_group(
    client: OpenAI,
    items: list[dict[str, Any]],
    rels: list[dict[str, Any]],
    stats: CallStats,
    *,
    repair_notes: str = "",
) -> dict[str, Any]:
    user = (
        "【片段清单】\n"
        + format_item_catalog(items)
        + "\n【片段间关系】\n"
        + format_relations(rels)
        + "\n\n请产出 entities + assignments + orphans。"
        + "每个片段 id 必须出现在 assignments 中。"
    )
    if repair_notes:
        user += f"\n\n【上轮校验/归组问题，请修正】\n{repair_notes}"
    data = _chat_json(
        client,
        system=STAGE1_SYSTEM,
        user=user,
        temperature=TEMPERATURE,
        stats=stats,
        label="stage1.group",
    )
    return _normalize_stage1(data, {str(it["id"]) for it in items if it.get("id")})


def _normalize_stage1(data: dict[str, Any], valid_ids: set[str]) -> dict[str, Any]:
    assignments_raw = data.get("assignments") or []
    if not isinstance(assignments_raw, list):
        raise ValueError("stage1 assignments 不是数组")

    assignment_map: dict[str, dict[str, Any]] = {}
    for row in assignments_raw:
        if not isinstance(row, dict):
            continue
        iid = str(row.get("item_id") or "").strip()
        if not iid:
            continue
        assignment_map[iid] = {
            "dest_kind": str(row.get("dest_kind") or "").strip() or "unassigned",
            "dest_id": str(row.get("dest_id") or "").strip(),
            "reason": str(row.get("reason") or "").strip(),
        }

    # 补洞：LLM 漏掉的片段强制标 unassigned，保证映射键完整（逆投影①）
    for iid in sorted(valid_ids):
        if iid not in assignment_map:
            assignment_map[iid] = {
                "dest_kind": "unassigned",
                "dest_id": "",
                "reason": "阶段1未分配，机械补为 unassigned",
            }

    entities_raw = data.get("entities") or []
    entities: list[dict[str, Any]] = []
    if isinstance(entities_raw, list):
        for ent in entities_raw:
            if not isinstance(ent, dict):
                continue
            kind = str(ent.get("kind") or "").strip()
            eid = str(ent.get("id") or "").strip()
            if not kind or not eid:
                continue
            item_ids = [
                str(x).strip() for x in (ent.get("item_ids") or []) if str(x).strip() in valid_ids
            ]
            entities.append(
                {
                    "kind": kind,
                    "id": eid,
                    "title": str(ent.get("title") or "").strip(),
                    "item_ids": item_ids,
                }
            )

    # 若 entities 空或缺，从 assignment_map 反推
    if not entities:
        buckets: dict[tuple[str, str], list[str]] = {}
        for iid, info in assignment_map.items():
            kind = info["dest_kind"]
            dest_id = info["dest_id"] or iid
            if kind in ("unassigned",):
                continue
            buckets.setdefault((kind, dest_id), []).append(iid)
        for (kind, dest_id), iids in buckets.items():
            entities.append(
                {
                    "kind": kind,
                    "id": dest_id,
                    "title": dest_id,
                    "item_ids": iids,
                }
            )

    orphans_raw = data.get("orphans") or []
    orphans: list[dict[str, Any]] = []
    if isinstance(orphans_raw, list):
        for o in orphans_raw:
            if isinstance(o, dict) and o.get("item_id"):
                orphans.append(
                    {
                        "item_id": str(o["item_id"]),
                        "why": str(o.get("why") or ""),
                    }
                )
    for iid, info in assignment_map.items():
        if info["dest_kind"] == "unassigned" and not any(o["item_id"] == iid for o in orphans):
            orphans.append({"item_id": iid, "why": info.get("reason") or "unassigned"})

    return {
        "entities": entities,
        "assignments": [{"item_id": k, **v} for k, v in sorted(assignment_map.items())],
        "assignment_map": assignment_map,
        "orphans": orphans,
    }


# ── 阶段 2：实体成形 ──────────────────────────────────────────

STAGE2_NODE_SYSTEM = """\
你是模组预处理流水线的「实体成形·节点」阶段。

任务：根据归到同一 node 的片段原文，合成 ScenarioModule.nodes[] 里的对象。
严格遵守目标 schema。skill 必须用 COC7 中文技能名（侦察、聆听、图书馆使用、\
话术、恐吓、说服、心理学、潜行、格斗：斗殴、射击：手枪……）。

图边必须分开填（只能引用已知 node id）：
- exits：空间上相邻的房间/地点
- contains：本节点内的物件/子区域 id（若那些 id 也在已知 node 列表中）
- leads_to：情节或因果上推进到的下一调查点（不是房间邻接表）
大段地点描述里若有可独立寻址的物件/子区，写入 sub_nodes[]（带独立 id），\
不要只堆在 kp_text。
有玩家可见摘要时填 public_text。
kp_text 可含 KP 绝密信息。不要发明原文没有的关键事实。

只输出 JSON：{"nodes": [ ... ]}
"""

STAGE2_NPC_SYSTEM = """\
你是模组预处理流水线的「实体成形·NPC」阶段。

任务：根据归到同一 npc 的片段，合成 npcs[] 对象。
stats 用自由字典（可含 STR/CON/SIZ/DEX/INT/POW/HP/DB/护甲等原文数据）。
战斗/属性数据优先写入 stats 或 forms[].stats，不要只留在旁路叙述。
同一实体的多形态（成体/幼体等）写入 forms[]；若公开形象与秘密身份是两行 npc，\
在 same_as 里写上对方 id（若已知）。
可填 public_text 作公开摘要。不要发明原文没有的数值。

只输出 JSON：{"npcs": [ ... ]}
"""

STAGE2_ENDING_SYSTEM = """\
你是模组预处理流水线的「实体成形·结局」阶段。

任务：根据结局相关片段合成 endings[]。
每条需要 id/title/text；condition 与 trigger 能从原文抽出就填，否则可省略。

只输出 JSON：{"endings": [ ... ]}
"""

STAGE2_AGENDA_SYSTEM = """\
你是模组预处理流水线的「实体成形·议程」阶段。

任务：根据议程相关片段合成 agenda[]。
trigger 必须是自由文本（描述何时触发），不要用枚举。
once 默认 true。
若原文写了触发后的可执行后果（路线关闭、时间推进、场景变化等），填入 effects[]，\
不要总给空列表。

只输出 JSON：{"agenda": [ ... ]}
"""


def _bundle_entity_materials(
    entity: dict[str, Any],
    items_by_id: dict[str, dict[str, Any]],
    lines: list[str],
    rels: list[dict[str, Any]],
) -> str:
    parts: list[str] = [
        f"实体 kind={entity['kind']} id={entity['id']} title={entity.get('title') or ''}",
        f"包含片段：{', '.join(entity.get('item_ids') or [])}",
        "",
    ]
    id_set = set(entity.get("item_ids") or [])
    for iid in entity.get("item_ids") or []:
        it = items_by_id.get(iid)
        if it is None:
            continue
        body = _item_body(lines, it)
        parts.append(f"### 片段 {iid}")
        parts.append(f"title: {it.get('title')}")
        parts.append(f"audience: {it.get('audience')}")
        parts.append(f"kind: {it.get('what_kind_of_thing')}")
        parts.append(f"summary: {it.get('summary')}")
        parts.append("原文：")
        parts.append(body)
        parts.append("")
    # 相关关系
    related = [r for r in rels if r["item_a"] in id_set or r["item_b"] in id_set]
    if related:
        parts.append("### 相关关系")
        parts.append(format_relations(related))
    return "\n".join(parts)


def stage2_form_kind(
    client: OpenAI,
    *,
    kind: str,
    entities: list[dict[str, Any]],
    items_by_id: dict[str, dict[str, Any]],
    lines: list[str],
    rels: list[dict[str, Any]],
    known_node_ids: list[str],
    stats: CallStats,
    schema_doc: str,
    repair_notes: str = "",
) -> list[dict[str, Any]]:
    targets = [e for e in entities if e.get("kind") == kind]
    if not targets:
        print(f"  stage2.{kind}: 无实体，跳过", flush=True)
        return []

    system = {
        "node": STAGE2_NODE_SYSTEM,
        "npc": STAGE2_NPC_SYSTEM,
        "ending": STAGE2_ENDING_SYSTEM,
        "agenda": STAGE2_AGENDA_SYSTEM,
    }.get(kind)
    if system is None:
        print(f"  stage2.{kind}: 非成形种类，跳过（留给阶段3）", flush=True)
        return []

    # 按实体逐个调用——窄合同，避免一次塞太多
    results: list[dict[str, Any]] = []
    key_name = {
        "node": "nodes",
        "npc": "npcs",
        "ending": "endings",
        "agenda": "agenda",
    }[kind]

    for ent in targets:
        materials = _bundle_entity_materials(ent, items_by_id, lines, rels)
        user = (
            f"{schema_doc}\n\n"
            "【已知全部 node id（exits/contains/leads_to 只能用这些）】\n"
            f"{', '.join(known_node_ids) or '（尚未固定，请用实体 id 列表）'}\n\n"
            f"【本实体材料】\n{materials}\n\n"
            f"请只产出这一个 {kind} 对象，放在 {key_name} 数组里（长度 1）。"
            f"id 必须是 {ent['id']!r}。"
        )
        if repair_notes:
            user += f"\n\n【校验问题请一并修正】\n{repair_notes}"
        data = _chat_json(
            client,
            system=system,
            user=user,
            temperature=TEMPERATURE,
            stats=stats,
            label=f"stage2.{kind}:{ent['id']}",
        )
        arr = data.get(key_name)
        if isinstance(arr, list):
            for obj in arr:
                if isinstance(obj, dict):
                    # 强制 id
                    obj["id"] = ent["id"]
                    if not obj.get("title"):
                        obj["title"] = ent.get("title") or ent["id"]
                    results.append(obj)
        elif isinstance(data.get(kind), dict):
            obj = data[kind]
            obj["id"] = ent["id"]
            results.append(obj)
        else:
            # 容错：响应本身就是实体
            if "id" in data or "kp_text" in data or "name" in data or "text" in data:
                data["id"] = ent["id"]
                results.append(data)
            else:
                print(f"    warn: stage2.{kind}:{ent['id']} 无法解析，跳过", flush=True)
    return results


# ── 阶段 3：顶层字段 ──────────────────────────────────────────

STAGE3_SYSTEM = """\
你是模组预处理流水线的「顶层字段」阶段。

任务：产出 meta / kp_truth / player_intro / opening / kp_guidance。

硬约束：
1. player_intro 与 opening.script **只能**使用玩家可见信息，禁止写入 KP 绝密真相。
2. kp_truth.summary 与 key_facts 放绝密；key_facts 里的措辞不要出现在 player_intro/opening.script。
3. kp_guidance 是 KP 主持用自由文本字典（键用英文蛇形或短中文均可）。
4. 不要输出 nodes/npcs/endings/agenda（那些已在其它阶段完成）。
5. **薄字段内容保全**：player_intro / opening 各自通常只对应 1 个归组片段——
   只用「标记为 player_intro / opening 的那一个片段」写它们的正文。
   **不要**把调查线索、报纸、行动描写并进 player_intro（那些应已在 nodes 里）。
6. kp_truth 只用标记为 kp_truth 的纯背景片段；
   不要把结局/议程材料写进 key_facts 代替 endings/agenda。
7. 只输出 JSON：
{"meta":{…},"kp_truth":{…},"player_intro":"…","opening":{…},"kp_guidance":{…}}
"""


def stage3_toplevel(
    client: OpenAI,
    *,
    items: list[dict[str, Any]],
    items_by_id: dict[str, dict[str, Any]],
    lines: list[str],
    stage1: dict[str, Any],
    schema_doc: str,
    stats: CallStats,
    module_title_hint: str,
    repair_notes: str = "",
) -> dict[str, Any]:
    # 收集顶层相关材料
    top_kinds = {
        "kp_truth",
        "kp_guidance",
        "opening",
        "player_intro",
        "meta",
        "unassigned",
    }
    amap: dict[str, dict[str, Any]] = stage1["assignment_map"]
    relevant_ids = [iid for iid, info in amap.items() if info.get("dest_kind") in top_kinds]
    # 也带上 audience 标明玩家可见 / 绝密 的摘要，便于分区
    parts: list[str] = [f"模组标题提示：{module_title_hint}", ""]
    for iid in relevant_ids:
        it = items_by_id.get(iid)
        if it is None:
            continue
        info = amap[iid]
        body = _item_body(lines, it)
        # 正文可能很长：顶层阶段截断单片段，避免爆上下文
        if len(body) > 2500:
            body = body[:2500] + "\n…(截断)"
        parts.append(f"### {iid} → {info.get('dest_kind')}/{info.get('dest_id')}")
        parts.append(f"audience: {it.get('audience')}")
        parts.append(f"summary: {it.get('summary')}")
        parts.append(body)
        parts.append("")

    # 额外：全部片段的 id/title/audience/summary 简表，防漏
    parts.append("### 全部片段简表（供补漏）")
    for it in items:
        parts.append(
            f"- {it.get('id')}: [{it.get('audience')}] {it.get('title')} — "
            f"{str(it.get('summary') or '')[:80]}"
        )

    user = (
        f"{schema_doc}\n\n"
        f"【顶层相关材料】\n" + "\n".join(parts) + "\n\n"
        "请产出 meta / kp_truth / player_intro / opening / kp_guidance。"
    )
    if repair_notes:
        user += f"\n\n【校验问题请修正——尤其注意不泄密】\n{repair_notes}"

    data = _chat_json(
        client,
        system=STAGE3_SYSTEM,
        user=user,
        temperature=TEMPERATURE,
        stats=stats,
        label="stage3.toplevel",
    )
    # 最低限度字段兜底
    raw_meta = data.get("meta")
    meta: dict[str, Any] = dict(raw_meta) if isinstance(raw_meta, dict) else {}
    if not meta.get("id"):
        meta["id"] = "assembled-module"
    if not meta.get("title"):
        meta["title"] = module_title_hint
    raw_truth = data.get("kp_truth")
    kp_truth: dict[str, Any] = dict(raw_truth) if isinstance(raw_truth, dict) else {}
    if not kp_truth.get("summary"):
        kp_truth["summary"] = "（组装层未给出摘要）"
    if not isinstance(kp_truth.get("key_facts"), list):
        kp_truth["key_facts"] = []
    player_intro = str(data.get("player_intro") or "").strip() or "（开场介绍待补）"
    raw_opening = data.get("opening")
    opening: dict[str, Any] | None = dict(raw_opening) if isinstance(raw_opening, dict) else None
    if opening is not None and not opening.get("script"):
        opening["script"] = player_intro
    raw_guidance = data.get("kp_guidance")
    guidance_src: dict[str, Any] = dict(raw_guidance) if isinstance(raw_guidance, dict) else {}
    kp_guidance = {str(k): str(v) for k, v in guidance_src.items()}

    return {
        "meta": meta,
        "kp_truth": kp_truth,
        "player_intro": player_intro,
        "opening": opening,
        "kp_guidance": kp_guidance,
    }


# ── 组装 + 自修 ──────────────────────────────────────────────


def _walk_node_dicts(nodes: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        out.append(n)
        sub = n.get("sub_node")
        if isinstance(sub, dict):
            out.extend(_walk_node_dicts([sub]))
        subs = n.get("sub_nodes")
        if isinstance(subs, list):
            out.extend(_walk_node_dicts([c for c in subs if isinstance(c, dict)]))
    return out


def mechanical_sanitize_module(module: dict[str, Any]) -> list[str]:
    """校验前的机械修补：去重 id、剪悬空边、清非法 same_as/pairs。

    整份 JSON 自修在大体量模组上常因输出截断失败；引用类错误应优先机械修。
    返回人类可读的修补日志。
    """
    notes: list[str] = []
    nodes = module.get("nodes")
    if not isinstance(nodes, list):
        return notes

    # 1) 扁平遍历，重命名重复 id（保留先出现的）
    seen: set[str] = set()

    def _dedupe_node(node: dict[str, Any], path: str) -> None:
        nid = str(node.get("id") or "node")
        if nid in seen:
            new_id = f"{path}-{nid}" if path else f"dup-{nid}"
            # 再撞则加序号
            base, i = new_id, 2
            while new_id in seen:
                new_id = f"{base}-{i}"
                i += 1
            notes.append(f"重命名重复 node id {nid!r} → {new_id!r}")
            node["id"] = new_id
            nid = new_id
        seen.add(nid)
        sub = node.get("sub_node")
        if isinstance(sub, dict):
            _dedupe_node(sub, nid)
        for child in node.get("sub_nodes") or []:
            if isinstance(child, dict):
                _dedupe_node(child, nid)

    for n in nodes:
        if isinstance(n, dict):
            _dedupe_node(n, "")

    node_ids = {str(n.get("id")) for n in _walk_node_dicts(nodes) if n.get("id")}
    npcs = module.get("npcs") if isinstance(module.get("npcs"), list) else []
    npc_ids = {str(n.get("id")) for n in npcs if isinstance(n, dict) and n.get("id")}

    # 2) 边只保留存在的 node id（npc 目标丢弃）
    for n in _walk_node_dicts(nodes):
        for key in ("leads_to", "exits", "contains"):
            raw = n.get(key)
            if not isinstance(raw, list):
                continue
            kept = [str(t) for t in raw if str(t) in node_ids]
            dropped = [str(t) for t in raw if str(t) not in node_ids]
            if dropped:
                notes.append(f"node {n.get('id')!r} 丢弃悬空 {key}: {dropped}")
            n[key] = kept

    # 3) same_as 只保留存在的 npc
    for npc in npcs:
        if not isinstance(npc, dict):
            continue
        raw = npc.get("same_as")
        if not isinstance(raw, list):
            continue
        kept = [str(t) for t in raw if str(t) in npc_ids]
        dropped = [str(t) for t in raw if str(t) not in npc_ids]
        if dropped:
            notes.append(f"npc {npc.get('id')!r} 丢弃悬空 same_as: {dropped}")
        npc["same_as"] = kept

    # 4) visibility_pairs 引用必须 ∈ node∪npc
    entity = node_ids | npc_ids
    pairs = module.get("visibility_pairs")
    if isinstance(pairs, list):
        cleaned: list[dict[str, Any]] = []
        for p in pairs:
            if not isinstance(p, dict):
                continue
            pref, sref = str(p.get("public_ref") or ""), str(p.get("secret_ref") or "")
            if pref in entity and sref in entity and pref != sref:
                cleaned.append(p)
            else:
                notes.append(f"丢弃非法 visibility_pair {p.get('id')!r} ({pref}↔{sref})")
        module["visibility_pairs"] = cleaned

    return notes


def compose_module(
    top: dict[str, Any],
    nodes: list[dict[str, Any]],
    npcs: list[dict[str, Any]],
    endings: list[dict[str, Any]],
    agenda: list[dict[str, Any]],
    visibility_pairs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    mod: dict[str, Any] = {
        "meta": top["meta"],
        "kp_truth": top["kp_truth"],
        "player_intro": top["player_intro"],
        "nodes": nodes,
        "npcs": npcs,
        "endings": endings,
        "agenda": agenda,
        "kp_guidance": top.get("kp_guidance") or {},
        "visibility_pairs": visibility_pairs or [],
    }
    if top.get("opening"):
        mod["opening"] = top["opening"]
    return mod


def _ensure_node_minimums(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保证每个 node 有 title/kp_text，缺则填占位，让 schema 能过。"""
    out: list[dict[str, Any]] = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        row = dict(n)
        row.setdefault("id", "unknown-node")
        row.setdefault("title", row["id"])
        if not row.get("kp_text"):
            row["kp_text"] = "（组装层未生成 kp_text）"
        row.setdefault("checks", [])
        row.setdefault("branches", [])
        row.setdefault("leads_to", [])
        row.setdefault("exits", [])
        row.setdefault("contains", [])
        row.setdefault("sub_nodes", [])
        if isinstance(row.get("sub_node"), dict):
            fixed_subs = _ensure_node_minimums([row["sub_node"]])
            row["sub_node"] = fixed_subs[0] if fixed_subs else row["sub_node"]
        if isinstance(row.get("sub_nodes"), list):
            row["sub_nodes"] = _ensure_node_minimums(
                [c for c in row["sub_nodes"] if isinstance(c, dict)]
            )
        out.append(row)
    return out


def _ensure_npc_minimums(npcs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in npcs:
        if not isinstance(n, dict):
            continue
        row = dict(n)
        row.setdefault("id", "unknown-npc")
        if not row.get("name"):
            row["name"] = row["id"]
        row.setdefault("forms", [])
        row.setdefault("same_as", [])
        out.append(row)
    return out


def _heuristic_visibility_pairs(
    nodes: list[dict[str, Any]],
    npcs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从 same_as 与 id 命名启发生成密级配对（无 LLM）。"""
    pairs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    npc_ids = {str(n.get("id")) for n in npcs if isinstance(n, dict) and n.get("id")}

    def _add(public_ref: str, secret_ref: str, note: str) -> None:
        if public_ref == secret_ref:
            return
        key = (public_ref, secret_ref)
        rev = (secret_ref, public_ref)
        if key in seen or rev in seen:
            return
        seen.add(key)
        pairs.append(
            {
                "id": f"pair-{public_ref}-{secret_ref}"[:64],
                "public_ref": public_ref,
                "secret_ref": secret_ref,
                "note": note,
            }
        )

    for npc in npcs:
        if not isinstance(npc, dict):
            continue
        nid = str(npc.get("id") or "")
        for other in npc.get("same_as") or []:
            other_s = str(other)
            if other_s in npc_ids:
                # 启发式：id 含 secret/public 时定向，否则双向记一条 public→other
                if "secret" in nid.lower() or "真相" in nid:
                    _add(other_s, nid, "same_as 启发")
                elif "public" in nid.lower() or "公开" in nid:
                    _add(nid, other_s, "same_as 启发")
                else:
                    _add(nid, other_s, "same_as 启发")

    # id 对：foo-public / foo-secret 或 foo-public-persona / foo-secret
    by_stem: dict[str, dict[str, str]] = {}
    for nid in npc_ids:
        low = nid.lower()
        stem = (
            low.replace("-public-persona", "")
            .replace("-public", "")
            .replace("-secret", "")
            .replace("_public", "")
            .replace("_secret", "")
        )
        slot = by_stem.setdefault(stem, {})
        if "public" in low or "公开" in nid:
            slot["public"] = nid
        if "secret" in low or "真相" in nid or "true" in low:
            slot["secret"] = nid
    for stem, slots in by_stem.items():
        if "public" in slots and "secret" in slots:
            _add(slots["public"], slots["secret"], f"id 命名启发:{stem}")

    # node 侧：*public* / *secret* 成对
    node_ids = [str(n.get("id")) for n in nodes if isinstance(n, dict) and n.get("id")]
    node_set = set(node_ids)
    for nid in node_ids:
        low = nid.lower()
        if "public" in low:
            cand = nid.lower().replace("public", "secret")
            # 保持原大小写尽量：简单扫描
            for other in node_set:
                if other.lower() == cand and other != nid:
                    _add(nid, other, "node id 命名启发")
    return pairs


def stage3b_visibility_pairs(
    client: OpenAI | None,
    *,
    nodes: list[dict[str, Any]],
    npcs: list[dict[str, Any]],
    rels: list[dict[str, Any]],
    stats: CallStats | None,
    schema_doc: str,
) -> list[dict[str, Any]]:
    """3b：密级配对。先启发式，可选 LLM 补洞（无 client 则只启发式）。"""
    pairs = _heuristic_visibility_pairs(nodes, npcs)
    if client is None or stats is None:
        return pairs

    # 关系里像「公开/假象/真相」的边，喂 LLM 一次补全
    hints: list[str] = []
    for r in rels:
        text = str(r.get("relation") or "")
        if any(k in text for k in ("公开", "假象", "真相", "配对", "表面", "实际")):
            hints.append(f"{r.get('item_a')} ↔ {r.get('item_b')}: {text[:120]}")
    if not hints and pairs:
        return pairs

    system = (
        "你是模组预处理流水线的「密级配对」阶段。根据已成形的 node/npc id 列表与"
        "关系提示，产出 visibility_pairs[]。"
        "public_ref 与 secret_ref 必须是已有 node 或 npc 的 id。"
        "不要发明 id。若无从配对，输出空数组。\n"
        '只输出 JSON：{"visibility_pairs": [...]}\n\n' + schema_doc
    )
    node_ids = [str(n.get("id")) for n in nodes if isinstance(n, dict) and n.get("id")]
    npc_ids = [str(n.get("id")) for n in npcs if isinstance(n, dict) and n.get("id")]
    user = (
        f"【node ids】{', '.join(node_ids)}\n"
        f"【npc ids】{', '.join(npc_ids)}\n"
        f"【已有启发式 pairs】{json.dumps(pairs, ensure_ascii=False)}\n"
        f"【关系提示】\n" + ("\n".join(hints[:40]) if hints else "（无）") + "\n"
        "请合并/补全 visibility_pairs（可保留启发式结果）。"
    )
    try:
        data = _chat_json(
            client,
            system=system,
            user=user,
            temperature=TEMPERATURE,
            stats=stats,
            label="stage3b.visibility_pairs",
        )
        arr = data.get("visibility_pairs")
        if isinstance(arr, list) and arr:
            cleaned: list[dict[str, Any]] = []
            entity = set(node_ids) | set(npc_ids)
            for p in arr:
                if not isinstance(p, dict):
                    continue
                pref = str(p.get("public_ref") or "")
                sref = str(p.get("secret_ref") or "")
                if pref in entity and sref in entity and pref != sref:
                    cleaned.append(
                        {
                            "id": str(p.get("id") or f"pair-{pref}-{sref}")[:64],
                            "public_ref": pref,
                            "secret_ref": sref,
                            "note": p.get("note"),
                        }
                    )
            if cleaned:
                return cleaned
    except Exception as exc:  # noqa: BLE001 — 配对失败不阻断管线
        print(f"  stage3b warn: {exc}", flush=True)
    return pairs


def _ensure_ending_minimums(endings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in endings:
        if not isinstance(e, dict):
            continue
        row = dict(e)
        row.setdefault("id", "unknown-ending")
        row.setdefault("title", row["id"])
        if not row.get("text"):
            row["text"] = "（组装层未生成结局文本）"
        out.append(row)
    return out


def _ensure_agenda_minimums(agenda: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in agenda:
        if not isinstance(a, dict):
            continue
        row = dict(a)
        row.setdefault("id", "unknown-agenda")
        if not row.get("trigger"):
            row["trigger"] = "（触发条件待补）"
        if not row.get("kp_text"):
            row["kp_text"] = "（议程内容待补）"
        row.setdefault("effects", [])
        row.setdefault("once", True)
        out.append(row)
    return out


def repair_module(
    client: OpenAI,
    *,
    module: dict[str, Any],
    report: ValidationReport,
    stats: CallStats,
    schema_doc: str,
    attempt: int,
) -> dict[str, Any]:
    """把错误清单 + 当前 JSON 喂回 LLM，要求整份修正。"""
    system = (
        "你是模组预处理流水线的「自修」阶段。收到一份 ScenarioModule JSON 与"
        "机械校验错误清单。请输出修正后的**完整** ScenarioModule JSON（不要只给 diff）。"
        "必须消除清单中的每一项错误。\n\n"
        "特别注意：\n"
        "- 技能名必须是 COC7 中文名（侦察 不是 侦查 也可，二者都接受；"
        "斗殴 应写作 格斗：斗殴；手枪 应写作 射击：手枪）。\n"
        "- leads_to / exits / contains 只能引用已存在的 node id；悬空引用请删除或改成存在的 id。\n"
        "- visibility_pairs 的 public_ref/secret_ref 必须是已有 node 或 npc id；悬空则删。\n"
        "- npc.same_as 只能引用已有 npc id。\n"
        "- 不泄密：从 key_facts 抽出的关键词不得出现在 player_intro 与 opening.script；"
        "可改写玩家可见字段，或把过细的关键词从 key_facts 改成更抽象的表述。\n"
        "- node.kp_text 可以含真相，不要为了「不泄密」去清空 kp_text。\n"
        "- 若 structure 报 endings/agenda 为空：从现有 kp_truth/node 文本抽出对应条目，"
        "补进 endings[] / agenda[]（不要只改 key_facts）。\n"
        "- 只输出一个 JSON 对象，即完整模组。\n\n" + schema_doc
    )
    user = (
        f"【校验错误】\n{report.summary_text()}\n\n"
        f"【当前模组 JSON】\n{json.dumps(module, ensure_ascii=False, indent=2)}"
    )
    data = _chat_json(
        client,
        system=system,
        user=user,
        temperature=TEMPERATURE,
        stats=stats,
        label=f"repair#{attempt}",
    )
    # 若模型包了一层
    if "meta" not in data and isinstance(data.get("module"), dict):
        data = data["module"]
    return data


def _print_stage1_summary(stage1: dict[str, Any]) -> None:
    print(
        f"  entities: {len(stage1['entities'])}, "
        f"assignments: {len(stage1['assignment_map'])}, "
        f"orphans: {len(stage1['orphans'])}",
        flush=True,
    )
    kind_counts: dict[str, int] = {}
    for info in stage1["assignment_map"].values():
        k = str(info.get("dest_kind") or "?")
        kind_counts[k] = kind_counts.get(k, 0) + 1
    print(f"  dest_kind 分布: {kind_counts}", flush=True)
    for ent in stage1["entities"]:
        print(
            f"    - {ent['kind']}:{ent['id']} ← {len(ent.get('item_ids') or [])} items",
            flush=True,
        )


def _run_stage2_and_3(
    client: OpenAI,
    *,
    stage1: dict[str, Any],
    items: list[dict[str, Any]],
    items_by_id: dict[str, dict[str, Any]],
    lines: list[str],
    rels: list[dict[str, Any]],
    schema_doc: str,
    title_hint: str,
    stats: CallStats,
    repair_notes: str = "",
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    """阶段2+3：返回 (top, nodes, npcs, endings, agenda, stage_meta)。"""
    known_node_ids = [e["id"] for e in stage1["entities"] if e.get("kind") == "node"]

    print("\n=== 阶段 2 · 实体成形 ===", flush=True)
    t2 = time.perf_counter()
    nodes = stage2_form_kind(
        client,
        kind="node",
        entities=stage1["entities"],
        items_by_id=items_by_id,
        lines=lines,
        rels=rels,
        known_node_ids=known_node_ids,
        stats=stats,
        schema_doc=schema_doc,
        repair_notes=repair_notes,
    )
    npcs = stage2_form_kind(
        client,
        kind="npc",
        entities=stage1["entities"],
        items_by_id=items_by_id,
        lines=lines,
        rels=rels,
        known_node_ids=known_node_ids,
        stats=stats,
        schema_doc=schema_doc,
        repair_notes=repair_notes,
    )
    endings = stage2_form_kind(
        client,
        kind="ending",
        entities=stage1["entities"],
        items_by_id=items_by_id,
        lines=lines,
        rels=rels,
        known_node_ids=known_node_ids,
        stats=stats,
        schema_doc=schema_doc,
        repair_notes=repair_notes,
    )
    agenda = stage2_form_kind(
        client,
        kind="agenda",
        entities=stage1["entities"],
        items_by_id=items_by_id,
        lines=lines,
        rels=rels,
        known_node_ids=known_node_ids,
        stats=stats,
        schema_doc=schema_doc,
        repair_notes=repair_notes,
    )
    nodes = _ensure_node_minimums(nodes)
    npcs = _ensure_npc_minimums(npcs)
    endings = _ensure_ending_minimums(endings)
    agenda = _ensure_agenda_minimums(agenda)
    print(
        f"  formed: nodes={len(nodes)} npcs={len(npcs)} "
        f"endings={len(endings)} agenda={len(agenda)}",
        flush=True,
    )
    stage2_meta = {
        "node_ids": [n.get("id") for n in nodes],
        "npc_ids": [n.get("id") for n in npcs],
        "ending_ids": [e.get("id") for e in endings],
        "agenda_ids": [a.get("id") for a in agenda],
        "elapsed_s": round(time.perf_counter() - t2, 2),
    }

    print("\n=== 阶段 3 · 顶层字段 ===", flush=True)
    t3 = time.perf_counter()
    top = stage3_toplevel(
        client,
        items=items,
        items_by_id=items_by_id,
        lines=lines,
        stage1=stage1,
        schema_doc=schema_doc,
        stats=stats,
        module_title_hint=title_hint,
        repair_notes=repair_notes,
    )
    stage3_meta = {
        "meta_id": top["meta"].get("id"),
        "key_facts_count": len(top["kp_truth"].get("key_facts") or []),
        "kp_guidance_keys": list((top.get("kp_guidance") or {}).keys()),
        "player_intro_len": len(top.get("player_intro") or ""),
        "elapsed_s": round(time.perf_counter() - t3, 2),
    }
    return top, nodes, npcs, endings, agenda, {"stage2": stage2_meta, "stage3": stage3_meta}


def _assignment_thin_counts(assignment_map: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for info in assignment_map.values():
        k = str(info.get("dest_kind") or "")
        if k in ("player_intro", "opening", "meta"):
            counts[k] = counts.get(k, 0) + 1
    return counts


def run_pipeline(
    *,
    extract_path: Path,
    pass1_path: Path,
    pass2_path: Path,
    source_txt: Path,
    example_path: Path | None,
    out_structured: Path,
    out_intermediate: Path,
    out_report: Path,
) -> int:
    print(f"extract: {extract_path}", flush=True)
    print(f"source:  {source_txt}", flush=True)

    extract = json.loads(extract_path.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = extract["items"]
    source_item_ids = {str(it["id"]) for it in items if it.get("id")}
    items_by_id = {str(it["id"]): it for it in items if it.get("id")}
    print(f"items: {len(items)}", flush=True)

    p1 = json.loads(pass1_path.read_text(encoding="utf-8"))
    p2 = json.loads(pass2_path.read_text(encoding="utf-8"))
    rels = merge_relations(p1.get("relations") or [], p2.get("relations") or [])
    print(f"relations merged: {len(rels)}", flush=True)

    lines = read_numbered_lines(source_txt)
    schema_doc = load_example_skeleton(example_path)
    title_hint = extract_path.name.split(".")[0]

    api_key = load_api_key()
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=180.0)
    stats = CallStats()
    t_all = time.perf_counter()

    intermediate: dict[str, Any] = {
        "source_extract": str(extract_path),
        "source_txt": str(source_txt),
        "item_count": len(items),
        "relation_count": len(rels),
    }

    # ── 阶段 1 ──
    print("\n=== 阶段 1 · 实体归组 ===", flush=True)
    t1 = time.perf_counter()
    stage1 = stage1_group(client, items, rels, stats)
    _print_stage1_summary(stage1)
    intermediate["stage1"] = {
        "entities": stage1["entities"],
        "assignments": stage1["assignments"],
        "orphans": stage1["orphans"],
        "thin_slot_counts": _assignment_thin_counts(stage1["assignment_map"]),
        "elapsed_s": round(time.perf_counter() - t1, 2),
    }

    # 若大量 unassigned，再给一次归组修正机会（不算自修次数）
    unassigned = [
        iid
        for iid, info in stage1["assignment_map"].items()
        if info.get("dest_kind") == "unassigned"
    ]
    if unassigned:
        print(
            f"  有 {len(unassigned)} 个 unassigned，再跑一次归组修正…",
            flush=True,
        )
        notes = "以下片段被标为 unassigned，请尽量归入合理实体：\n" + "\n".join(
            f"- {i}" for i in unassigned
        )
        stage1 = stage1_group(client, items, rels, stats, repair_notes=notes)
        _print_stage1_summary(stage1)
        intermediate["stage1"] = {
            "entities": stage1["entities"],
            "assignments": stage1["assignments"],
            "orphans": stage1["orphans"],
            "thin_slot_counts": _assignment_thin_counts(stage1["assignment_map"]),
            "elapsed_s": round(time.perf_counter() - t1, 2),
            "had_regroup": True,
        }

    top, nodes, npcs, endings, agenda, s23 = _run_stage2_and_3(
        client,
        stage1=stage1,
        items=items,
        items_by_id=items_by_id,
        lines=lines,
        rels=rels,
        schema_doc=schema_doc,
        title_hint=title_hint,
        stats=stats,
    )
    intermediate["stage2"] = s23["stage2"]
    intermediate["stage3"] = s23["stage3"]

    print("\n=== 阶段 3b · 密级配对 ===", flush=True)
    visibility_pairs = stage3b_visibility_pairs(
        client,
        nodes=nodes,
        npcs=npcs,
        rels=rels,
        stats=stats,
        schema_doc=schema_doc,
    )
    intermediate["stage3b"] = {"pair_count": len(visibility_pairs)}
    print(f"  visibility_pairs: {len(visibility_pairs)}", flush=True)

    module = compose_module(top, nodes, npcs, endings, agenda, visibility_pairs=visibility_pairs)
    n_skill = normalize_module_skills(module)
    if n_skill:
        print(f"  技能名机械归一：{n_skill} 处", flush=True)
    mech = mechanical_sanitize_module(module)
    if mech:
        print(f"  机械修补 {len(mech)} 项：", flush=True)
        for line in mech[:20]:
            print(f"    - {line}", flush=True)

    # ── 校验 + 自修 ──
    print("\n=== 校验闭环 ===", flush=True)
    assignment_map = stage1["assignment_map"]
    repair_count = 0
    report = validate_assembled(
        module,
        source_item_ids=source_item_ids,
        assignment_map=assignment_map,
        items=items,
    )
    print(report.summary_text(), flush=True)

    while not report.ok and repair_count < MAX_REPAIR:
        repair_count += 1
        print(f"\n--- 自修 #{repair_count} ---", flush=True)
        err_text = "\n".join(report.all_errors())

        if report.needs_stage1_repair():
            # 归组类失败：回灌阶段1，再重跑阶段2/3（06：错误喂回阶段1 重新归组）
            print("  → 归组类失败，回灌阶段1 重新归组…", flush=True)
            stage1 = stage1_group(client, items, rels, stats, repair_notes=err_text)
            _print_stage1_summary(stage1)
            intermediate["stage1"] = {
                "entities": stage1["entities"],
                "assignments": stage1["assignments"],
                "orphans": stage1["orphans"],
                "thin_slot_counts": _assignment_thin_counts(stage1["assignment_map"]),
                "elapsed_s": round(time.perf_counter() - t1, 2),
                f"repair_regroup_{repair_count}": True,
            }
            assignment_map = stage1["assignment_map"]
            top, nodes, npcs, endings, agenda, s23 = _run_stage2_and_3(
                client,
                stage1=stage1,
                items=items,
                items_by_id=items_by_id,
                lines=lines,
                rels=rels,
                schema_doc=schema_doc,
                title_hint=title_hint,
                stats=stats,
                repair_notes=err_text,
            )
            intermediate["stage2"] = s23["stage2"]
            intermediate["stage3"] = s23["stage3"]
            visibility_pairs = stage3b_visibility_pairs(
                client,
                nodes=nodes,
                npcs=npcs,
                rels=rels,
                stats=stats,
                schema_doc=schema_doc,
            )
            intermediate["stage3b"] = {"pair_count": len(visibility_pairs)}
            module = compose_module(
                top, nodes, npcs, endings, agenda, visibility_pairs=visibility_pairs
            )
            n_skill = normalize_module_skills(module)
            if n_skill:
                print(f"  技能名机械归一：{n_skill} 处", flush=True)
            mech = mechanical_sanitize_module(module)
            if mech:
                print(f"  机械修补 {len(mech)} 项", flush=True)
            report = validate_assembled(
                module,
                source_item_ids=source_item_ids,
                assignment_map=assignment_map,
                items=items,
            )
            print(report.summary_text(), flush=True)
            # 归组修好后若只剩产物级问题，同轮再修一次 JSON（不另计 repair）
            if report.ok or report.needs_stage1_repair():
                continue

        # schema/ref/skill/leak：先机械归一+修补，仍失败再尝试整份 JSON 自修
        print("  → 产物级失败，技能归一 + 机械修补…", flush=True)
        normalize_module_skills(module)
        mech = mechanical_sanitize_module(module)
        if mech:
            print(f"  机械修补 {len(mech)} 项", flush=True)
            for line in mech[:15]:
                print(f"    - {line}", flush=True)
        report = validate_assembled(
            module,
            source_item_ids=source_item_ids,
            assignment_map=assignment_map,
            items=items,
        )
        print(report.summary_text(), flush=True)
        if report.ok:
            continue

        # 仅非引用类错误才上 LLM 整份自修（大体量 JSON 易截断）
        hard = [e for e in report.all_errors() if not e.startswith("[ref]")]
        if not hard:
            print("  → 仅剩 ref 类错误且机械修补后仍在，跳过 LLM 自修", flush=True)
            continue

        print("  → 仍有非 ref 错误，尝试整份 JSON 自修…", flush=True)
        try:
            module = repair_module(
                client,
                module=module,
                report=report,
                stats=stats,
                schema_doc=schema_doc,
                attempt=repair_count,
            )
        except RuntimeError as exc:
            print(f"  → LLM 自修失败（保留当前产物继续）：{exc}", flush=True)
            break
        module["nodes"] = _ensure_node_minimums(module.get("nodes") or [])
        module["npcs"] = _ensure_npc_minimums(module.get("npcs") or [])
        module["endings"] = _ensure_ending_minimums(module.get("endings") or [])
        module["agenda"] = _ensure_agenda_minimums(module.get("agenda") or [])
        module.setdefault("visibility_pairs", [])
        normalize_module_skills(module)
        mechanical_sanitize_module(module)

        report = validate_assembled(
            module,
            source_item_ids=source_item_ids,
            assignment_map=assignment_map,
            items=items,
        )
        print(report.summary_text(), flush=True)

    elapsed_all = time.perf_counter() - t_all
    cost = stats.estimate_cost_cny()

    # 计数
    check_count = sum(len(n.get("checks") or []) for n in module.get("nodes") or [])
    thin_counts = _assignment_thin_counts(assignment_map)

    intermediate["validation"] = report.to_dict()
    intermediate["repair_count"] = repair_count
    intermediate["thin_slot_counts"] = thin_counts
    intermediate["content_preserve_suspect_count"] = len(report.content_preserve_suspects)
    intermediate["stats"] = {
        "elapsed_seconds": round(elapsed_all, 2),
        "calls": stats.calls,
        "prompt_tokens": stats.prompt_tokens,
        "completion_tokens": stats.completion_tokens,
        "total_tokens": stats.total_tokens,
        "estimated_cost_cny": round(cost, 4),
        "stage_logs": stats.stage_logs,
        "failures": stats.failures,
        "retries": stats.retries,
    }
    intermediate["counts"] = {
        "nodes": len(module.get("nodes") or []),
        "npcs": len(module.get("npcs") or []),
        "endings": len(module.get("endings") or []),
        "agenda": len(module.get("agenda") or []),
        "checks": check_count,
        "kp_guidance_keys": len(module.get("kp_guidance") or {}),
        "key_facts": len((module.get("kp_truth") or {}).get("key_facts") or []),
        "player_intro_items": thin_counts.get("player_intro", 0),
        "opening_items": thin_counts.get("opening", 0),
        "meta_items": thin_counts.get("meta", 0),
        "content_preserve_suspects": len(report.content_preserve_suspects),
    }
    intermediate["success"] = report.ok

    # 写产物（模组资料/，gitignored）
    out_structured.parent.mkdir(parents=True, exist_ok=True)
    out_structured.write_text(
        json.dumps(module, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    out_intermediate.write_text(
        json.dumps(intermediate, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    out_report.write_text(
        json.dumps(
            {
                "success": report.ok,
                "repair_count": repair_count,
                "report": report.to_dict(),
                "counts": intermediate["counts"],
                "thin_slot_counts": thin_counts,
                "stats": intermediate["stats"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print("\n=== 完成 ===", flush=True)
    print(f"wrote: {out_structured}", flush=True)
    print(f"wrote: {out_intermediate}", flush=True)
    print(f"wrote: {out_report}", flush=True)
    print(f"success: {report.ok}", flush=True)
    print(f"repair_count: {repair_count}", flush=True)
    print(f"counts: {intermediate['counts']}", flush=True)
    print(f"thin_slot_counts: {thin_counts}", flush=True)
    print(
        f"content_preserve_suspects: {len(report.content_preserve_suspects)}",
        flush=True,
    )
    print(
        f"calls={stats.calls} elapsed={elapsed_all:.1f}s "
        f"tokens={stats.total_tokens} cost≈¥{cost:.4f}",
        flush=True,
    )
    for log in stats.stage_logs:
        print(
            f"  · {log['label']}: {log['elapsed_s']}s "
            f"in={log['prompt_tokens']} out={log['completion_tokens']}",
            flush=True,
        )

    # 校验不过不假装成功
    return 0 if report.ok else 2


def default_paths(extract: Path) -> tuple[Path, Path, Path]:
    name = extract.name
    base = name[: -len(".裸抽取.json")] if name.endswith(".裸抽取.json") else extract.stem
    parent = extract.parent
    return (
        parent / f"{base}.structured.json",
        parent / f"{base}.组装中间态.json",
        parent / f"{base}.校验报告.json",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="预处理管线组装层 + 校验闭环")
    parser.add_argument("--extract", type=Path, required=True, help="裸抽取 JSON")
    parser.add_argument("--relations-pass1", type=Path, required=True)
    parser.add_argument("--relations-pass2", type=Path, required=True)
    parser.add_argument("--source-txt", type=Path, required=True, help="模组原文 txt")
    parser.add_argument(
        "--example",
        type=Path,
        default=None,
        help="格式范例 structured.json（仅取骨架，内容不进产物）",
    )
    parser.add_argument("--out", type=Path, default=None, help="输出 structured.json")
    parser.add_argument("--out-intermediate", type=Path, default=None)
    parser.add_argument("--out-report", type=Path, default=None)
    args = parser.parse_args(argv)

    structured, intermediate, report = default_paths(args.extract)
    if args.out:
        structured = args.out
    if args.out_intermediate:
        intermediate = args.out_intermediate
    if args.out_report:
        report = args.out_report

    return run_pipeline(
        extract_path=args.extract,
        pass1_path=args.relations_pass1,
        pass2_path=args.relations_pass2,
        source_txt=args.source_txt,
        example_path=args.example,
        out_structured=structured,
        out_intermediate=intermediate,
        out_report=report,
    )


if __name__ == "__main__":
    raise SystemExit(main())
