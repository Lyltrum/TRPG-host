"""关系发现探针 + chunk 字段补抽——契约发现第 2 步。

承接裸抽取（probe.py）。第一轮只得到扁平条目（id 互引 = 0）；
本脚本补两件事：

  A. 给缺失 what_kind_of_thing 的条目补字段（不重抽条目、不给候选词）
  B. 让模型用自由文本说出条目之间的联系（不暗示 leads_to / 邻接 / 方向）

用法（在 trpg-backend/ 下）：

    .venv/bin/python scripts/module_probe/relation_probe.py \\
        --extract ../模组资料/某模组.裸抽取.json \\
        --source-txt ../模组资料/某模组.txt

产物默认写到抽取 JSON 同目录（gitignored）：
  - 原地更新 *.裸抽取.json（补 what_kind_of_thing）
  - *.关系-pass1.json / *.关系-pass2.json
  - *.关系对拍.md

分批模式（--batch-size N，可选 --output-suffix）：
  - 每批约 N 条作为「要为之找关系的对象」，同时附全部条目 id+title 简表
  - 产物默认带后缀，例如 *.关系-pass1-分批.json，不覆盖全量跑的原始数据
  - RELATION_SYSTEM 与核心问法不变；只拆批次、合并去重

脚本只吃路径参数，不内嵌任何模组正文。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import OpenAI

# 与 probe.py 同目录：保证 `python scripts/module_probe/relation_probe.py` 可 import
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from probe import (  # noqa: E402 — 脚本同目录导入
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    load_api_key,
    read_numbered_lines,
)

MAX_RETRIES = 3

# ── A. 字段补抽 ──────────────────────────────────────────────
# 只问「这是一段什么东西」。不给候选词、不给已有描述清单。
BACKFILL_SYSTEM = """\
你在阅读一份桌面角色扮演游戏模组文稿中的一条已切分条目。

任务：只回答一个问题——用你自己的话说，这是一段什么东西。

输出格式：只输出一个 JSON 对象，形如
{"what_kind_of_thing": "……"}
不要 markdown 围栏，不要其它键。

约束：
- 自由描述，不要从任何固定类别列表里挑
- 不要发明原文没有的信息
- 只描述「这是什么东西」，不要写摘要复述
"""

# ── B. 关系发现 ──────────────────────────────────────────────
# 核心：问「哪两条之间存在联系、联系是什么」，不暗示有向链/图/依赖。
# 禁止在 prompt 里出现：通向、邻接、前置、依赖、指向、from/to、leads_to 等。
RELATION_SYSTEM = """\
你在阅读一份桌面角色扮演游戏模组文稿已被切分成的全部条目清单。

任务：指出哪些条目之间存在联系，并用你自己的话描述每一对之间的联系是什么。

什么叫「联系」：两条条目在内容上彼此有关——例如讲的是同一件事的不同侧面、\
一条是另一条的背景、两条描述同一对象、一条的内容会影响到另一条的理解，等等。\
请按你读到的实际内容判断，不要套用任何预设的关系分类表。

输出格式：只输出一个 JSON 对象，形如
{"relations": [
  {"item_a": "条目id", "item_b": "条目id", "relation": "用你自己的话描述这两者之间是什么联系"}
]}
不要 markdown 围栏。

字段纪律：
- item_a 与 item_b 都是清单里的 id，无先后含义（不要理解成起点/终点）
- relation 是自由文本，不要从固定列表里挑
- 不要输出方向布尔值、不要输出关系强度、不要输出类型枚举

重要：
- 某个条目跟其它任何条目都没有联系，是完全正常的答案——不要为了「看起来完整」\
硬造联系
- 不要发明清单里没有的 id
- 同一对条目只写一条（不要 a-b 与 b-a 各写一次）
- 若你认为整份清单里几乎没有跨条目联系，relations 可以为空数组
"""

PASS_TEMPERATURES = (0.3, 0.5)


@dataclass
class CallStats:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    retries: list[dict[str, Any]] = field(default_factory=list)


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
            if usage is not None:
                stats.prompt_tokens += usage.prompt_tokens or 0
                stats.completion_tokens += usage.completion_tokens or 0
                stats.total_tokens += usage.total_tokens or 0
            raw = response.choices[0].message.content or ""
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError(f"响应不是 JSON 对象：{type(data).__name__}")
            print(
                f"  {label}: ok ({elapsed:.1f}s, attempt {attempt}, temp={temperature})",
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
        except Exception as exc:  # noqa: BLE001 — 探针记失败并重试
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
    """按 line_start/line_end 取出原文行区间（仅运行时使用，不写进 git）。"""
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


def backfill_missing_kinds(
    client: OpenAI,
    payload: dict[str, Any],
    lines: list[str],
    stats: CallStats,
    *,
    chunk_index: int | None = None,
) -> list[dict[str, Any]]:
    """只补 what_kind_of_thing，不改条目边界与其它字段。

    默认补**所有**缺失条目；若给 chunk_index 则只补该块（兼容科比特先生那次
    只修 chunk 2 的用法）。每条独立调用，且不附带任何其它条目的已有描述——
    避免往已有类上靠。
    """
    items: list[dict[str, Any]] = payload["items"]
    targets = [
        it
        for it in items
        if not str(it.get("what_kind_of_thing") or "").strip()
        and (chunk_index is None or it.get("chunk_index") == chunk_index)
    ]
    scope = f"chunk_index={chunk_index}" if chunk_index is not None else "全部块"
    print(
        f"A · 字段补抽：{scope} 且 what_kind_of_thing 为空 → {len(targets)} 条",
        flush=True,
    )
    results: list[dict[str, Any]] = []
    for it in targets:
        item_id = it.get("id")
        body = _item_body(lines, it)
        user = (
            f"条目 id：{item_id}\n"
            f"标题：{it.get('title') or ''}\n"
            f"摘要：{it.get('summary') or ''}\n"
            f"\n对应原文：\n{body}\n"
            f"\n请用你自己的话说：这是一段什么东西？"
        )
        data = _chat_json(
            client,
            system=BACKFILL_SYSTEM,
            user=user,
            temperature=0.2,
            stats=stats,
            label=f"backfill:{item_id}",
        )
        kind = data.get("what_kind_of_thing")
        if kind is None or not str(kind).strip():
            raise RuntimeError(f"补抽 {item_id} 返回空 what_kind_of_thing: {data!r}")
        kind_str = str(kind).strip()
        # 原地更新：只动 kind 与来源标记
        it["what_kind_of_thing"] = kind_str
        it["what_kind_of_thing_source"] = "backfill"
        results.append({"id": item_id, "what_kind_of_thing": kind_str})
        print(f"    ← {item_id}: {kind_str}", flush=True)
    return results


def _format_catalog(items: list[dict[str, Any]]) -> str:
    """关系发现用的全局清单：id / title / what_kind_of_thing / summary。"""
    lines: list[str] = []
    for it in items:
        lines.append(f"- id: {it.get('id')}")
        lines.append(f"  title: {it.get('title') or ''}")
        lines.append(f"  what_kind_of_thing: {it.get('what_kind_of_thing') or ''}")
        lines.append(f"  summary: {it.get('summary') or ''}")
        lines.append("")
    return "\n".join(lines)


def _format_brief_catalog(items: list[dict[str, Any]]) -> str:
    """分批模式：全部条目的 id + title 简表（不含 summary，省 token）。"""
    lines: list[str] = []
    for it in items:
        lines.append(f"- id: {it.get('id')}  title: {it.get('title') or ''}")
    return "\n".join(lines)


def _normalize_relations(
    raw: dict[str, Any],
    valid_ids: set[str],
) -> list[dict[str, Any]]:
    """机械清洗：只要合法 id 对 + 非空 relation；不改措辞、不做语义归并。"""
    rels = raw.get("relations")
    if rels is None:
        raise ValueError("响应缺少 relations 键")
    if not isinstance(rels, list):
        raise ValueError(f"relations 不是数组：{type(rels).__name__}")

    out: list[dict[str, Any]] = []
    seen_pairs: set[frozenset[str]] = set()
    for row in rels:
        if not isinstance(row, dict):
            continue
        a = str(row.get("item_a") or "").strip()
        b = str(row.get("item_b") or "").strip()
        rel = str(row.get("relation") or "").strip()
        if not a or not b or not rel:
            continue
        if a not in valid_ids or b not in valid_ids:
            print(f"    skip unknown id pair: {a!r} / {b!r}", flush=True)
            continue
        if a == b:
            print(f"    skip self-pair: {a!r}", flush=True)
            continue
        pair = frozenset({a, b})
        if pair in seen_pairs:
            # 同遍内 a-b / b-a 去重，保留先出现的 relation 原文
            continue
        seen_pairs.add(pair)
        out.append({"item_a": a, "item_b": b, "relation": rel})
    return out


def _chunk_items(items: list[dict[str, Any]], batch_size: int) -> list[list[dict[str, Any]]]:
    """按原顺序切成约 batch_size 的焦点批。"""
    if batch_size <= 0:
        raise ValueError(f"batch_size 必须为正整数，得到 {batch_size}")
    return [items[i : i + batch_size] for i in range(0, len(items), batch_size)]


def merge_batch_relations(
    batch_rows: list[tuple[int, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """跨批合并去重。

    - 同一无序对 + 同一 relation 原文 → 只保留一条，source_batches 合并
    - 同一无序对但 relation 描述不同 → 都保留，各自标注来源批次
    不做语义归并。
    """
    # pair -> relation_text -> sorted unique batch indices
    buckets: dict[frozenset[str], dict[str, list[int]]] = {}
    order: list[tuple[frozenset[str], str, str, str]] = []  # pair, rel, a, b

    for batch_idx, rels in batch_rows:
        for r in rels:
            a = r["item_a"]
            b = r["item_b"]
            rel = r["relation"]
            pair = frozenset({a, b})
            if pair not in buckets:
                buckets[pair] = {}
            if rel not in buckets[pair]:
                buckets[pair][rel] = []
                order.append((pair, rel, a, b))
            if batch_idx not in buckets[pair][rel]:
                buckets[pair][rel].append(batch_idx)

    out: list[dict[str, Any]] = []
    for pair, rel, a, b in order:
        batches = sorted(buckets[pair][rel])
        out.append(
            {
                "item_a": a,
                "item_b": b,
                "relation": rel,
                "source_batches": batches,
            }
        )
    return out


def discover_relations(
    client: OpenAI,
    items: list[dict[str, Any]],
    stats: CallStats,
    *,
    temperature: float,
    pass_name: str,
) -> list[dict[str, Any]]:
    """全量一次调用（原路径）。"""
    valid_ids = {str(it["id"]) for it in items if it.get("id")}
    catalog = _format_catalog(items)
    user = (
        f"以下是全部 {len(items)} 条条目（id / title / what_kind_of_thing / summary）。\n"
        f"请指出哪些条目之间存在联系，并用你自己的话描述每一对之间的联系。\n\n"
        f"{catalog}"
    )
    print(
        f"B · 关系发现 {pass_name}：{len(items)} 条条目，temp={temperature}",
        flush=True,
    )
    data = _chat_json(
        client,
        system=RELATION_SYSTEM,
        user=user,
        temperature=temperature,
        stats=stats,
        label=f"relations:{pass_name}",
    )
    rels = _normalize_relations(data, valid_ids)
    print(f"  {pass_name}: {len(rels)} relations after normalize", flush=True)
    return rels


def discover_relations_batched(
    client: OpenAI,
    items: list[dict[str, Any]],
    stats: CallStats,
    *,
    temperature: float,
    pass_name: str,
    batch_size: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """分批关系发现：问法（RELATION_SYSTEM + 核心句）不变，只拆焦点批。

    每批：
      - 附全部条目 id+title 简表（可引用批外 id）
      - 附本批对象的完整字段（id/title/what_kind_of_thing/summary）
      - 核心任务句与全量路径相同
    """
    valid_ids = {str(it["id"]) for it in items if it.get("id")}
    batches = _chunk_items(items, batch_size)
    brief = _format_brief_catalog(items)
    batch_rows: list[tuple[int, list[dict[str, Any]]]] = []
    batch_meta: list[dict[str, Any]] = []

    print(
        f"B · 关系发现 {pass_name}（分批）：{len(items)} 条条目，"
        f"{len(batches)} 批 × 约 {batch_size}，temp={temperature}",
        flush=True,
    )

    for bi, focus in enumerate(batches):
        focus_catalog = _format_catalog(focus)
        # 核心任务句与 discover_relations 全量路径保持一致；仅增加简表 + 本批对象结构。
        user = (
            f"以下是全部 {len(items)} 条条目的 id / title 简表"
            f"（不含 summary；item_a/item_b 可用其中任意 id）：\n\n"
            f"{brief}\n\n"
            f"以下是本批要为之找关系的对象共 {len(focus)} 条"
            f"（id / title / what_kind_of_thing / summary）。\n"
            f"请指出哪些条目之间存在联系，并用你自己的话描述每一对之间的联系。\n\n"
            f"{focus_catalog}"
        )
        data = _chat_json(
            client,
            system=RELATION_SYSTEM,
            user=user,
            temperature=temperature,
            stats=stats,
            label=f"relations:{pass_name}:batch{bi}",
        )
        rels = _normalize_relations(data, valid_ids)
        print(
            f"  {pass_name} batch {bi}/{len(batches) - 1}: "
            f"focus={len(focus)} → {len(rels)} relations",
            flush=True,
        )
        batch_rows.append((bi, rels))
        batch_meta.append(
            {
                "batch_index": bi,
                "focus_count": len(focus),
                "focus_ids": [str(it.get("id")) for it in focus],
                "relation_count": len(rels),
            }
        )

    merged = merge_batch_relations(batch_rows)
    # 对拍用无序对：同一对多 relation 描述只计一次 pair
    unique_pairs = {pair_key(r) for r in merged}
    print(
        f"  {pass_name}: merged {len(merged)} rows, {len(unique_pairs)} unique pairs",
        flush=True,
    )
    meta = {
        "batch_size": batch_size,
        "batch_count": len(batches),
        "batches": batch_meta,
        "merged_row_count": len(merged),
        "unique_pair_count": len(unique_pairs),
    }
    return merged, meta


def pair_key(rel: dict[str, Any]) -> frozenset[str]:
    """无序对键——对拍只比这对，不比方向、不做语义归并。"""
    return frozenset({rel["item_a"], rel["item_b"]})


def unique_relations_by_pair(rels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对拍前按无序对去重（保留先出现的 relation 原文）。不做语义归并。"""
    out: list[dict[str, Any]] = []
    seen: set[frozenset[str]] = set()
    for r in rels:
        k = pair_key(r)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def compare_passes(
    pass1: list[dict[str, Any]],
    pass2: list[dict[str, Any]],
) -> dict[str, Any]:
    """机械对拍：无序对是否相同；同一对 relation 字符串是否字面相等。"""
    # 分批合并后同一对可能有多条不同 relation；对拍按无序对计一次
    p1 = unique_relations_by_pair(pass1)
    p2 = unique_relations_by_pair(pass2)
    map1: dict[frozenset[str], str] = {pair_key(r): r["relation"] for r in p1}
    map2: dict[frozenset[str], str] = {pair_key(r): r["relation"] for r in p2}
    keys1 = set(map1)
    keys2 = set(map2)
    inter = keys1 & keys2
    only1 = keys1 - keys2
    only2 = keys2 - keys1
    same_pair_diff_rel = sorted(
        [
            {
                "item_a": sorted(k)[0],
                "item_b": sorted(k)[1],
                "relation_pass1": map1[k],
                "relation_pass2": map2[k],
            }
            for k in inter
            if map1[k] != map2[k]
        ],
        key=lambda x: (x["item_a"], x["item_b"]),
    )
    same_pair_same_rel = sorted(
        [
            {
                "item_a": sorted(k)[0],
                "item_b": sorted(k)[1],
                "relation": map1[k],
            }
            for k in inter
            if map1[k] == map2[k]
        ],
        key=lambda x: (x["item_a"], x["item_b"]),
    )
    return {
        # 条数 = 无序对去重后；row_count 含「同对多 relation 描述」的额外行
        "pass1_count": len(p1),
        "pass2_count": len(p2),
        "pass1_row_count": len(pass1),
        "pass2_row_count": len(pass2),
        "intersection_count": len(inter),
        "union_count": len(keys1 | keys2),
        "only_pass1_count": len(only1),
        "only_pass2_count": len(only2),
        "diff_set_count": len(only1) + len(only2),
        "same_pair_same_relation_count": len(same_pair_same_rel),
        "same_pair_diff_relation_count": len(same_pair_diff_rel),
        "intersection_pairs": sorted(
            [{"item_a": sorted(k)[0], "item_b": sorted(k)[1]} for k in inter],
            key=lambda x: (x["item_a"], x["item_b"]),
        ),
        "only_pass1": sorted(
            [
                {
                    "item_a": r["item_a"],
                    "item_b": r["item_b"],
                    "relation": r["relation"],
                }
                for r in p1
                if pair_key(r) in only1
            ],
            key=lambda x: (x["item_a"], x["item_b"]),
        ),
        "only_pass2": sorted(
            [
                {
                    "item_a": r["item_a"],
                    "item_b": r["item_b"],
                    "relation": r["relation"],
                }
                for r in p2
                if pair_key(r) in only2
            ],
            key=lambda x: (x["item_a"], x["item_b"]),
        ),
        "same_pair_same_relation": same_pair_same_rel,
        "same_pair_diff_relation": same_pair_diff_rel,
    }


def isolation_stats(
    items: list[dict[str, Any]],
    pass1: list[dict[str, Any]],
    pass2: list[dict[str, Any]],
) -> dict[str, Any]:
    """关系密度：并集上「至少参与一条关系」的条目；完全孤立者。"""
    involved: set[str] = set()
    for r in pass1 + pass2:
        involved.add(r["item_a"])
        involved.add(r["item_b"])
    all_ids = [str(it["id"]) for it in items if it.get("id")]
    id_to_kind = {
        str(it["id"]): str(it.get("what_kind_of_thing") or "") for it in items if it.get("id")
    }
    isolated = [i for i in all_ids if i not in involved]
    return {
        "total_items": len(all_ids),
        "involved_count": len(involved),
        "isolated_count": len(isolated),
        "isolated": [{"id": i, "what_kind_of_thing": id_to_kind.get(i, "")} for i in isolated],
    }


def kind_inventory(items: list[dict[str, Any]]) -> list[tuple[str, int, bool]]:
    """what_kind_of_thing 去重清单：(文本, 计数, 是否含补抽来源)。"""
    counter: Counter[str] = Counter()
    backfill_kinds: set[str] = set()
    for it in items:
        kind = str(it.get("what_kind_of_thing") or "").strip() or "(空)"
        counter[kind] += 1
        if it.get("what_kind_of_thing_source") == "backfill":
            backfill_kinds.add(kind)
    rows: list[tuple[str, int, bool]] = []
    for kind, count in counter.most_common():
        rows.append((kind, count, kind in backfill_kinds))
    return rows


def relation_inventory(
    pass1: list[dict[str, Any]],
    pass2: list[dict[str, Any]],
) -> list[tuple[str, int]]:
    """relation 字段去重清单——原样合并计数，不做语义归类。"""
    counter: Counter[str] = Counter()
    for r in pass1 + pass2:
        counter[r["relation"]] += 1
    return list(counter.most_common())


def write_compare_md(
    path: Path,
    cmp: dict[str, Any],
    iso: dict[str, Any],
    kinds: list[tuple[str, int, bool]],
    rel_types: list[tuple[str, int]],
    stats: CallStats,
    elapsed: float,
    backfilled: list[dict[str, Any]],
    *,
    batch_info: dict[str, Any] | None = None,
) -> None:
    out: list[str] = []
    out.append("# 关系发现探针 · 对拍报告")
    out.append("")
    out.append("对拍是机械比较（无序对是否相同），不做语义归并。")
    out.append("")
    if batch_info is not None:
        out.append("## 分批参数")
        out.append("")
        out.append(f"- batch_size：{batch_info.get('batch_size')}")
        out.append(f"- 批次数：{batch_info.get('batch_count')}")
        sizes = batch_info.get("batch_focus_sizes") or []
        if sizes:
            out.append(f"- 每批焦点条目数：{sizes}")
        out.append("- 模式：分批（问法与 RELATION_SYSTEM 未改）")
        out.append("")
    out.append("## 调用统计")
    out.append("")
    out.append(f"- 耗时：{elapsed:.1f}s")
    out.append(f"- 调用次数：{stats.calls}")
    out.append(
        f"- token：prompt={stats.prompt_tokens}, "
        f"completion={stats.completion_tokens}, total={stats.total_tokens}"
    )
    out.append(f"- 失败：{len(stats.failures)}")
    out.append(f"- 重试后成功：{len(stats.retries)}")
    if stats.failures:
        for f in stats.failures:
            out.append(f"  - {f}")
    if stats.retries:
        for r in stats.retries:
            out.append(f"  - {r}")
    out.append("")
    out.append("## A · 字段补抽")
    out.append("")
    out.append(f"- 补抽条数：{len(backfilled)}")
    for row in backfilled:
        out.append(f"- `{row['id']}` → {row['what_kind_of_thing']}")
    out.append("")
    out.append("## what_kind_of_thing 去重清单（补抽后）")
    out.append("")
    for kind, count, is_bf in kinds:
        tag = " **[含补抽]**" if is_bf else ""
        out.append(f"- ({count}) {kind}{tag}")
    out.append("")
    out.append("## 对拍统计")
    out.append("")
    out.append(f"- pass1 关系数（无序对）：{cmp['pass1_count']}")
    out.append(f"- pass2 关系数（无序对）：{cmp['pass2_count']}")
    if (
        cmp.get("pass1_row_count", cmp["pass1_count"]) != cmp["pass1_count"]
        or cmp.get("pass2_row_count", cmp["pass2_count"]) != cmp["pass2_count"]
    ):
        out.append(f"- pass1 行数（含同对多描述）：{cmp.get('pass1_row_count')}")
        out.append(f"- pass2 行数（含同对多描述）：{cmp.get('pass2_row_count')}")
    out.append(f"- 交集（无序对两遍都有）：{cmp['intersection_count']}")
    default_union = cmp["intersection_count"] + cmp["diff_set_count"]
    out.append(f"- 并集：{cmp.get('union_count', default_union)}")
    union = cmp.get("union_count") or default_union
    rate = (cmp["intersection_count"] / union * 100.0) if union else 0.0
    out.append(f"- 交集率（交集/并集）：{rate:.1f}%")
    out.append(f"- 差集（只出现一遍）：{cmp['diff_set_count']}")
    out.append(f"  - 仅 pass1：{cmp['only_pass1_count']}")
    out.append(f"  - 仅 pass2：{cmp['only_pass2_count']}")
    out.append(f"- 同一对且 relation 字面相同：{cmp['same_pair_same_relation_count']}")
    out.append(f"- 同一对但 relation 描述不同：{cmp['same_pair_diff_relation_count']}")
    out.append("")
    out.append("### 同一对但描述不同（最有价值）")
    out.append("")
    if not cmp["same_pair_diff_relation"]:
        out.append("（无）")
    else:
        for row in cmp["same_pair_diff_relation"]:
            out.append(f"- `{row['item_a']}` ↔ `{row['item_b']}`")
            out.append(f"  - pass1: {row['relation_pass1']}")
            out.append(f"  - pass2: {row['relation_pass2']}")
    out.append("")
    out.append("### 仅 pass1")
    out.append("")
    if not cmp["only_pass1"]:
        out.append("（无）")
    else:
        for row in cmp["only_pass1"]:
            out.append(f"- `{row['item_a']}` ↔ `{row['item_b']}`: {row['relation']}")
    out.append("")
    out.append("### 仅 pass2")
    out.append("")
    if not cmp["only_pass2"]:
        out.append("（无）")
    else:
        for row in cmp["only_pass2"]:
            out.append(f"- `{row['item_a']}` ↔ `{row['item_b']}`: {row['relation']}")
    out.append("")
    out.append("## relation 去重清单（两遍合计，原样）")
    out.append("")
    for rel, count in rel_types:
        out.append(f"- ({count}) {rel}")
    out.append("")
    out.append("## 关系密度")
    out.append("")
    out.append(f"- 条目总数：{iso['total_items']}")
    out.append(f"- 至少参与一条关系（两遍并集）：{iso['involved_count']}")
    out.append(f"- 完全孤立：{iso['isolated_count']}")
    out.append("")
    out.append("### 完全孤立的条目")
    out.append("")
    if not iso["isolated"]:
        out.append("（无）")
    else:
        for row in iso["isolated"]:
            out.append(f"- `{row['id']}` · {row['what_kind_of_thing']}")
    out.append("")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def default_paths(
    extract_path: Path,
    *,
    output_suffix: str = "",
) -> tuple[Path, Path, Path]:
    """从 *.裸抽取.json 推导关系产物路径。

    output_suffix 非空时插入到扩展名前，例如 suffix='分批' →
    *.关系-pass1-分批.json / *.关系对拍-分批.md，避免覆盖全量原始数据。
    """
    name = extract_path.name
    base = name[: -len(".裸抽取.json")] if name.endswith(".裸抽取.json") else extract_path.stem
    parent = extract_path.parent
    suf = f"-{output_suffix}" if output_suffix else ""
    return (
        parent / f"{base}.关系-pass1{suf}.json",
        parent / f"{base}.关系-pass2{suf}.json",
        parent / f"{base}.关系对拍{suf}.md",
    )


def resolve_source_txt(extract_path: Path, payload: dict[str, Any], explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    src = payload.get("source")
    if src:
        p = Path(str(src))
        if p.is_file():
            return p.resolve()
    # 约定：同目录下把 .裸抽取.json 换成 .txt
    name = extract_path.name
    if name.endswith(".裸抽取.json"):
        candidate = extract_path.parent / f"{name[: -len('.裸抽取.json')]}.txt"
        if candidate.is_file():
            return candidate.resolve()
    raise SystemExit("无法定位模组纯文本，请用 --source-txt 指定")


def run(
    extract_path: Path,
    source_txt: Path | None,
    *,
    skip_backfill: bool = False,
    skip_relations: bool = False,
    backfill_chunk: int | None = None,
    batch_size: int | None = None,
    output_suffix: str = "",
) -> int:
    print(f"extract: {extract_path}", flush=True)
    payload = json.loads(extract_path.read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = payload["items"]
    print(f"items: {len(items)}", flush=True)
    if batch_size is not None:
        print(f"batch_size: {batch_size}", flush=True)
    if output_suffix:
        print(f"output_suffix: {output_suffix}", flush=True)

    lines: list[str] = []
    if not skip_backfill:
        txt_path = resolve_source_txt(extract_path, payload, source_txt)
        print(f"source_txt: {txt_path}", flush=True)
        lines = read_numbered_lines(txt_path)
        print(f"lines: {len(lines)}", flush=True)
    else:
        print("source_txt: (skipped, backfill off)", flush=True)

    pass1_path, pass2_path, md_path = default_paths(extract_path, output_suffix=output_suffix)
    api_key = load_api_key()
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=180.0)
    stats = CallStats()
    backfilled: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    batch_meta_pass1: dict[str, Any] | None = None
    batch_meta_pass2: dict[str, Any] | None = None

    # ── A ──
    if not skip_backfill:
        backfilled = backfill_missing_kinds(
            client,
            payload,
            lines,
            stats,
            chunk_index=backfill_chunk,
        )
        extract_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote (backfill in-place): {extract_path}", flush=True)
        still_empty = [
            it.get("id")
            for it in payload["items"]
            if not str(it.get("what_kind_of_thing") or "").strip()
        ]
        if still_empty:
            print(f"WARNING: still empty what_kind_of_thing: {still_empty}", flush=True)
    else:
        print("A · skip backfill", flush=True)

    # ── B ──
    pass1: list[dict[str, Any]] = []
    pass2: list[dict[str, Any]] = []
    if not skip_relations:
        items = payload["items"]
        use_batch = batch_size is not None and batch_size > 0

        if use_batch:
            assert batch_size is not None
            pass1, batch_meta_pass1 = discover_relations_batched(
                client,
                items,
                stats,
                temperature=PASS_TEMPERATURES[0],
                pass_name="pass1",
                batch_size=batch_size,
            )
        else:
            pass1 = discover_relations(
                client,
                items,
                stats,
                temperature=PASS_TEMPERATURES[0],
                pass_name="pass1",
            )
        pass1_payload: dict[str, Any] = {
            "pass": 1,
            "temperature": PASS_TEMPERATURES[0],
            "source_extract": str(extract_path),
            "item_count": len(items),
            "mode": "batched" if use_batch else "full",
            "relations": pass1,
        }
        if batch_meta_pass1 is not None:
            pass1_payload["batch"] = batch_meta_pass1
        pass1_path.write_text(
            json.dumps(pass1_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote: {pass1_path}", flush=True)

        if use_batch:
            assert batch_size is not None
            pass2, batch_meta_pass2 = discover_relations_batched(
                client,
                items,
                stats,
                temperature=PASS_TEMPERATURES[1],
                pass_name="pass2",
                batch_size=batch_size,
            )
        else:
            pass2 = discover_relations(
                client,
                items,
                stats,
                temperature=PASS_TEMPERATURES[1],
                pass_name="pass2",
            )
        pass2_payload: dict[str, Any] = {
            "pass": 2,
            "temperature": PASS_TEMPERATURES[1],
            "source_extract": str(extract_path),
            "item_count": len(items),
            "mode": "batched" if use_batch else "full",
            "relations": pass2,
        }
        if batch_meta_pass2 is not None:
            pass2_payload["batch"] = batch_meta_pass2
        pass2_path.write_text(
            json.dumps(pass2_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote: {pass2_path}", flush=True)
    else:
        print("B · skip relations", flush=True)

    elapsed = time.perf_counter() - t0
    items = payload["items"]
    cmp = compare_passes(pass1, pass2) if (pass1 or pass2) else compare_passes([], [])
    iso = isolation_stats(items, pass1, pass2)
    kinds = kind_inventory(items)
    rel_types = relation_inventory(pass1, pass2)

    batch_info: dict[str, Any] | None = None
    if batch_meta_pass1 is not None:
        sizes = [b["focus_count"] for b in batch_meta_pass1.get("batches", [])]
        batch_info = {
            "batch_size": batch_meta_pass1.get("batch_size"),
            "batch_count": batch_meta_pass1.get("batch_count"),
            "batch_focus_sizes": sizes,
        }

    write_compare_md(
        md_path,
        cmp,
        iso,
        kinds,
        rel_types,
        stats,
        elapsed,
        backfilled,
        batch_info=batch_info,
    )
    print(f"wrote: {md_path}", flush=True)

    # ── 终端汇报原料（元信息 only）──
    print("", flush=True)
    print("======== REPORT ========", flush=True)
    print(f"elapsed_s: {elapsed:.1f}", flush=True)
    print(f"calls: {stats.calls}", flush=True)
    print(
        f"tokens: prompt={stats.prompt_tokens} "
        f"completion={stats.completion_tokens} total={stats.total_tokens}",
        flush=True,
    )
    print(f"failures: {stats.failures}", flush=True)
    print(f"retries: {stats.retries}", flush=True)
    print(f"backfilled: {len(backfilled)}", flush=True)
    if batch_info is not None:
        print(f"batch_size: {batch_info.get('batch_size')}", flush=True)
        print(f"batch_count: {batch_info.get('batch_count')}", flush=True)
        print(f"batch_focus_sizes: {batch_info.get('batch_focus_sizes')}", flush=True)
    print("", flush=True)
    print("## what_kind_of_thing 去重清单", flush=True)
    for kind, count, is_bf in kinds:
        tag = " [含补抽]" if is_bf else ""
        print(f"({count}) {kind}{tag}", flush=True)
    print("", flush=True)
    print("## relation 去重清单（两遍合计）", flush=True)
    for rel, count in rel_types:
        print(f"({count}) {rel}", flush=True)
    print("", flush=True)
    print("## 对拍", flush=True)
    print(f"pass1: {cmp['pass1_count']}", flush=True)
    print(f"pass2: {cmp['pass2_count']}", flush=True)
    print(f"pass1_rows: {cmp.get('pass1_row_count', cmp['pass1_count'])}", flush=True)
    print(f"pass2_rows: {cmp.get('pass2_row_count', cmp['pass2_count'])}", flush=True)
    print(f"intersection: {cmp['intersection_count']}", flush=True)
    union = cmp.get("union_count", cmp["intersection_count"] + cmp["diff_set_count"])
    print(f"union: {union}", flush=True)
    rate = (cmp["intersection_count"] / union * 100.0) if union else 0.0
    print(f"intersection_rate: {rate:.1f}%", flush=True)
    print(f"diff_set: {cmp['diff_set_count']}", flush=True)
    print(f"same_pair_diff_relation: {cmp['same_pair_diff_relation_count']}", flush=True)
    print("", flush=True)
    print("## 关系密度", flush=True)
    print(f"involved: {iso['involved_count']} / {iso['total_items']}", flush=True)
    print(f"isolated: {iso['isolated_count']}", flush=True)
    for row in iso["isolated"]:
        print(f"  - {row['id']} · {row['what_kind_of_thing']}", flush=True)

    if stats.failures:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="关系发现探针 + 字段补抽（契约发现）")
    parser.add_argument(
        "--extract",
        required=True,
        type=Path,
        help="裸抽取 JSON 路径（*.裸抽取.json）",
    )
    parser.add_argument(
        "--source-txt",
        type=Path,
        default=None,
        help="模组纯文本路径（默认从 JSON 的 source 或同目录推导）",
    )
    parser.add_argument(
        "--skip-backfill",
        action="store_true",
        help="跳过 A 部分字段补抽",
    )
    parser.add_argument(
        "--skip-relations",
        action="store_true",
        help="跳过 B 部分关系发现",
    )
    parser.add_argument(
        "--backfill-chunk",
        type=int,
        default=None,
        help="只补该 chunk_index 的空字段（默认：所有块中缺失的）",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="分批关系发现：每批焦点条目数（默认不分批，全量一次调用）",
    )
    parser.add_argument(
        "--output-suffix",
        type=str,
        default="",
        help="产物文件名后缀，例如 分批 → *.关系-pass1-分批.json（不覆盖全量产物）",
    )
    args = parser.parse_args(argv)
    extract_path = args.extract.expanduser().resolve()
    if not extract_path.is_file():
        print(f"抽取文件不存在: {extract_path}", file=sys.stderr)
        return 2
    if args.batch_size is not None and args.batch_size <= 0:
        print(f"--batch-size 必须为正整数，得到 {args.batch_size}", file=sys.stderr)
        return 2
    return run(
        extract_path,
        args.source_txt,
        skip_backfill=args.skip_backfill,
        skip_relations=args.skip_relations,
        backfill_chunk=args.backfill_chunk,
        batch_size=args.batch_size,
        output_suffix=args.output_suffix,
    )


if __name__ == "__main__":
    raise SystemExit(main())
