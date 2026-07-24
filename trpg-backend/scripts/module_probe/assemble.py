"""预处理管线 · 组装层 + 校验闭环（路线第 4a 步 = B）。

把探针产物（裸抽取 + 关系）组装成现有 ScenarioModule 形状的结构化 JSON，
供 keeper **一行不改**就能主持。

与探针的关键区别：本步**刻意给出目标 schema**（B 的定义）。
与 A 的边界：不产出三层新结构，不改 app/core/keeper/。

阶段（窄合同多次调用，不压成一次干所有事）：
  1. 实体归组 —— 片段 id → 归宿（node/npc/ending/agenda/kp_truth/…）
  2. 实体成形 —— 按归宿合成 node/npc/ending/agenda 对象
  3. 顶层字段 —— meta / kp_truth / player_intro / opening / kp_guidance
  然后机械五项校验 + 自修重试 ≤2 次。

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
from validate_module import ValidationReport, validate_assembled  # noqa: E402

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
      "kp_text": "KP 视角完整信息（可含真相）",
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
      "leads_to": ["其它 node id"]
    }
  ],
  "npcs": [
    {
      "id": "英文短 id",
      "name": "显示名",
      "role": "可选",
      "kp_notes": "KP 笔记",
      "stats": {"STR": 50, "HP": 10}
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
  "kp_guidance": {"键": "自由文本指引"}
}
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
- node —— 调查场景/地点/可到达节点（会进入 nodes[]）
- npc —— 人物或怪物（会进入 npcs[]）
- ending —— 结局相关（会进入 endings[]）
- agenda —— 不依赖玩家行动、世界自己会推进的事件（会进入 agenda[]）
- kp_truth —— 顶层 KP 绝密真相（背景真相、核心秘密）
- kp_guidance —— 顶层 KP 主持指引（节奏、战斗警告、跑偏处理等）
- opening —— 开场脚本素材
- player_intro —— 玩家开场介绍素材（仅玩家可见信息）
- meta —— 模组元信息（标题、人数等）

规则：
1. **无孤儿**：每个输入片段 id 必须出现在 assignments 里恰好一次。
2. 被聚合/层级/包含类关系连在一起的片段，应归入同一实体（同一 dest_kind + dest_id）。
3. dest_id 用英文小写连字符；同一实体的多片段共享同一个 dest_id。
4. 分不掉的片段 dest_kind 用 "unassigned"，并在 orphans 里说明原因——禁止静默丢弃。
5. 不要发明清单里没有的片段 id。
6. 只输出 JSON，不要 markdown 围栏。

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
leads_to 只能引用已知的 node id 列表中的 id。
kp_text 可含 KP 绝密信息。不要发明原文没有的关键事实。

只输出 JSON：{"nodes": [ ... ]}
"""

STAGE2_NPC_SYSTEM = """\
你是模组预处理流水线的「实体成形·NPC」阶段。

任务：根据归到同一 npc 的片段，合成 npcs[] 对象。
stats 用自由字典（可含 STR/CON/SIZ/DEX/INT/POW/HP/DB/护甲等原文数据）。
不要发明原文没有的数值。

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
            "【已知全部 node id（leads_to 只能用这些）】\n"
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
5. 只输出 JSON：
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


def compose_module(
    top: dict[str, Any],
    nodes: list[dict[str, Any]],
    npcs: list[dict[str, Any]],
    endings: list[dict[str, Any]],
    agenda: list[dict[str, Any]],
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
        out.append(row)
    return out


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
        "- leads_to 只能引用已存在的 node id；悬空引用请删除或改成存在的 id。\n"
        "- 不泄密：从 key_facts 抽出的关键词不得出现在 player_intro 与 opening.script；"
        "可改写玩家可见字段，或把过细的关键词从 key_facts 改成更抽象的表述。\n"
        "- node.kp_text 可以含真相，不要为了「不泄密」去清空 kp_text。\n"
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
    print(
        f"  entities: {len(stage1['entities'])}, "
        f"assignments: {len(stage1['assignment_map'])}, "
        f"orphans: {len(stage1['orphans'])}",
        flush=True,
    )
    for ent in stage1["entities"]:
        print(
            f"    - {ent['kind']}:{ent['id']} ← {len(ent.get('item_ids') or [])} items",
            flush=True,
        )
    intermediate["stage1"] = {
        "entities": stage1["entities"],
        "assignments": stage1["assignments"],
        "orphans": stage1["orphans"],
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
        intermediate["stage1"] = {
            "entities": stage1["entities"],
            "assignments": stage1["assignments"],
            "orphans": stage1["orphans"],
            "elapsed_s": round(time.perf_counter() - t1, 2),
            "had_regroup": True,
        }

    known_node_ids = [e["id"] for e in stage1["entities"] if e.get("kind") == "node"]

    # ── 阶段 2 ──
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
    intermediate["stage2"] = {
        "node_ids": [n.get("id") for n in nodes],
        "npc_ids": [n.get("id") for n in npcs],
        "ending_ids": [e.get("id") for e in endings],
        "agenda_ids": [a.get("id") for a in agenda],
        "elapsed_s": round(time.perf_counter() - t2, 2),
    }

    # ── 阶段 3 ──
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
    )
    intermediate["stage3"] = {
        "meta_id": top["meta"].get("id"),
        "key_facts_count": len(top["kp_truth"].get("key_facts") or []),
        "kp_guidance_keys": list((top.get("kp_guidance") or {}).keys()),
        "elapsed_s": round(time.perf_counter() - t3, 2),
    }

    module = compose_module(top, nodes, npcs, endings, agenda)

    # ── 校验 + 自修 ──
    print("\n=== 校验闭环 ===", flush=True)
    assignment_map = stage1["assignment_map"]
    repair_count = 0
    report = validate_assembled(
        module,
        source_item_ids=source_item_ids,
        assignment_map=assignment_map,
    )
    print(report.summary_text(), flush=True)

    while not report.ok and repair_count < MAX_REPAIR:
        repair_count += 1
        print(f"\n--- 自修 #{repair_count} ---", flush=True)
        module = repair_module(
            client,
            module=module,
            report=report,
            stats=stats,
            schema_doc=schema_doc,
            attempt=repair_count,
        )
        # 自修后仍强制 minimums
        module["nodes"] = _ensure_node_minimums(module.get("nodes") or [])
        module["npcs"] = _ensure_npc_minimums(module.get("npcs") or [])
        module["endings"] = _ensure_ending_minimums(module.get("endings") or [])
        module["agenda"] = _ensure_agenda_minimums(module.get("agenda") or [])
        report = validate_assembled(
            module,
            source_item_ids=source_item_ids,
            assignment_map=assignment_map,
        )
        print(report.summary_text(), flush=True)

    elapsed_all = time.perf_counter() - t_all
    cost = stats.estimate_cost_cny()

    # 计数
    check_count = sum(len(n.get("checks") or []) for n in module.get("nodes") or [])

    intermediate["validation"] = report.to_dict()
    intermediate["repair_count"] = repair_count
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
