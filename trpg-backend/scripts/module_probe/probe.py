"""模组「裸抽取」探针——契约发现第 1 步。

目的不是把模组解析好，而是在**不给任何目标 schema** 的前提下，看一份
模组文稿自然吐出什么形状（见 docs/keeper-design/exec/02-… 执行规格）。

用法（在 trpg-backend/ 下）：

    .venv/bin/python scripts/module_probe/probe.py \\
        --input ../模组资料/某模组.txt

产物默认写到输入文件同目录：`*.裸抽取.json`、`*.覆盖率.md`。
脚本只吃路径参数，不内嵌任何模组正文。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import OpenAI

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"
TEMPERATURE = 0.2
MAX_CHUNK_CHARS = 6000
CONTEXT_CHARS = 200
MAX_RETRIES = 3

# 允许出现的概念只有「这块在讲什么」和「给谁看」。
# 禁止：节点/NPC/检定/线索/结局/分支/leads_to/schema/字段 等目标结构词。
SYSTEM_PROMPT = """\
你在阅读一份桌面角色扮演游戏的模组文稿的一块内容。

任务：把这块内容里各自成一件事、可独立理解的信息拆成条目列出来。
不要漏掉；也不要为了整齐而硬塞进某种分类。

对每个条目输出这些键：
- id：你自己起的短标识（英文小写连字符）
- title：一句话标题
- what_kind_of_thing：用你自己的话说，这是一段什么东西（自由描述，不要套用预设类别）
- summary：这条在讲什么，两三句
- audience：谁能看到这段内容（自由描述。常见情况是玩家可见或 KP 绝密，\
但也可能是别的情况，按内容如实写，不要强迫二选一）
- line_start：本条目对应正文的起始行号（整数，必须落在本块行号范围内）
- line_end：本条目对应正文的结束行号（整数，必须落在本块行号范围内）

输出格式：只输出一个 JSON 对象，形如 {"items": [ ... ]}，不要 markdown 围栏。

约束：
- 行号已标在每行开头，形如 [123]|正文；line_start/line_end 必须是真实行号
- 若提供了「前文上下文」「后文上下文」，那两段只用于理解衔接，\
不要为它们产出任何条目
- what_kind_of_thing 与 audience 都是自由文本，不要从任何固定列表里挑
- 不要发明原文没有的信息
"""


@dataclass
class Chunk:
    index: int
    line_start: int  # 1-based inclusive
    line_end: int  # 1-based inclusive
    text: str
    char_count: int
    prev_context: str = ""
    next_context: str = ""


@dataclass
class ExtractStats:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    failed_chunks: list[dict[str, Any]] = field(default_factory=list)
    retried_chunks: list[dict[str, Any]] = field(default_factory=list)


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_api_key() -> str:
    """读 DEEPSEEK_API_KEY：先看环境变量，再读 trpg-backend/.env。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key
    env_path = _backend_root() / ".env"
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == "DEEPSEEK_API_KEY":
                key = value.strip().strip('"').strip("'")
                if key:
                    return key
    raise SystemExit("未找到 DEEPSEEK_API_KEY（环境变量或 trpg-backend/.env）")


def read_numbered_lines(path: Path) -> list[str]:
    """读入文本，返回 0-based 行列表（对应 1-based 行号 = index+1）。"""
    text = path.read_text(encoding="utf-8")
    # 保留末尾空行语义：splitlines 会丢掉最后的空行，补回以匹配行号约定
    lines = text.splitlines()
    if text.endswith("\n") and (not lines or lines[-1] != ""):
        # 普通情况 splitlines 已够用；仅在文件以 \n 结尾时行数与编辑器一致
        pass
    return lines


def _is_heading_like(line: str) -> bool:
    """启发式：短、非空、不像正文句子——常是小节标题。

    仅用于优先在标题前断块，不假设任何固定标题词表。
    """
    s = line.strip()
    if not s or len(s) > 40:
        return False
    # 属性表/数值行不当标题
    if any(ch.isdigit() for ch in s) and any(
        tok in s for tok in ("STR", "CON", "SIZ", "%", "：", ":")
    ):
        return False
    # 以破折号/括号注释起头的通常是脚注而非新节
    return not s.startswith(("—", "——", "（", "("))


def _segment_starts(lines: list[str]) -> list[int]:
    """结构边界：空行后的非空行，或紧跟空行之后的短标题行起点。

    返回每个结构段的起始 0-based 下标。段内可含多行正文；
    分块时优先整段打包，超限再在段内按行断开。
    """
    n = len(lines)
    starts = [0] if n else []
    for i in range(1, n):
        prev_empty = not lines[i - 1].strip()
        cur_empty = not lines[i].strip()
        if cur_empty:
            continue
        # 空行之后的内容起新段
        if prev_empty:
            starts.append(i)
            continue
        # 非空行之后紧跟短标题：也视为新段（无空行的小标题）
        if _is_heading_like(lines[i]) and lines[i - 1].strip() and len(lines[i - 1].strip()) > 40:
            starts.append(i)
    return starts


def build_chunks(lines: list[str], max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    """按结构边界分块：优先整段（空行/标题前），硬上限 max_chars。

    行是原子单位；单行超过上限时仍整行放入（不断句）。
    """
    n = len(lines)
    if n == 0:
        return []

    seg_starts = _segment_starts(lines)
    # 展开为 (start, end_exclusive) 段
    segments: list[tuple[int, int]] = []
    for si, start in enumerate(seg_starts):
        end = seg_starts[si + 1] if si + 1 < len(seg_starts) else n
        segments.append((start, end))

    def span_chars(a: int, b: int) -> int:
        """[a, b) 行拼成文本的字符数（行间换行）。"""
        if b <= a:
            return 0
        return sum(len(lines[k]) for k in range(a, b)) + (b - a - 1)

    def split_oversized(seg_a: int, seg_b: int) -> list[tuple[int, int]]:
        """单段超限时按行边界切开。"""
        parts: list[tuple[int, int]] = []
        line_a = seg_a
        while line_a < seg_b:
            line_b = line_a
            used = 0
            while line_b < seg_b:
                add = len(lines[line_b]) if line_b == line_a else 1 + len(lines[line_b])
                if line_b > line_a and used + add > max_chars:
                    break
                used += add
                line_b += 1
            if line_b == line_a:
                line_b = line_a + 1
            parts.append((line_a, line_b))
            line_a = line_b
        return parts

    # 贪心打包段；单段超限则按行切
    chunk_ranges: list[tuple[int, int]] = []
    cur_a: int | None = None
    cur_b = 0
    cur_chars = 0

    def flush() -> None:
        nonlocal cur_a, cur_b, cur_chars
        if cur_a is not None and cur_b > cur_a:
            chunk_ranges.append((cur_a, cur_b))
        cur_a, cur_b, cur_chars = None, 0, 0

    def start_with_segment(seg_a: int, seg_b: int, seg_len: int) -> None:
        nonlocal cur_a, cur_b, cur_chars
        if seg_len <= max_chars:
            cur_a, cur_b, cur_chars = seg_a, seg_b, seg_len
        else:
            for part in split_oversized(seg_a, seg_b):
                chunk_ranges.append(part)

    for seg_a, seg_b in segments:
        seg_len = span_chars(seg_a, seg_b)
        if cur_a is None:
            start_with_segment(seg_a, seg_b, seg_len)
            continue
        # 两段相邻，拼接点一个换行
        joined = cur_chars + 1 + seg_len
        if joined <= max_chars:
            cur_b = seg_b
            cur_chars = joined
        else:
            flush()
            start_with_segment(seg_a, seg_b, seg_len)
    flush()

    chunks: list[Chunk] = []
    for idx, (start, end) in enumerate(chunk_ranges):
        body_lines = lines[start:end]
        text = "\n".join(body_lines)
        chunks.append(
            Chunk(
                index=idx,
                line_start=start + 1,
                line_end=end,  # exclusive 0-based → inclusive 1-based
                text=text,
                char_count=len(text),
            )
        )

    for idx, chunk in enumerate(chunks):
        if idx > 0:
            prev = chunks[idx - 1].text
            chunk.prev_context = prev[-CONTEXT_CHARS:] if len(prev) > CONTEXT_CHARS else prev
        if idx + 1 < len(chunks):
            nxt = chunks[idx + 1].text
            chunk.next_context = nxt[:CONTEXT_CHARS] if len(nxt) > CONTEXT_CHARS else nxt
    return chunks


def format_chunk_for_prompt(chunk: Chunk, lines: list[str]) -> str:
    parts: list[str] = []
    if chunk.prev_context:
        parts.append("【前文上下文——只用于理解，不要为它产出条目】")
        parts.append(chunk.prev_context)
        parts.append("")
    parts.append(f"【本块正文 行 {chunk.line_start}–{chunk.line_end}——请只为本块产出条目】")
    for lineno in range(chunk.line_start, chunk.line_end + 1):
        content = lines[lineno - 1]
        parts.append(f"[{lineno}]|{content}")
    if chunk.next_context:
        parts.append("")
        parts.append("【后文上下文——只用于理解，不要为它产出条目】")
        parts.append(chunk.next_context)
    return "\n".join(parts)


def _parse_items_payload(raw: str) -> list[dict[str, Any]]:
    data = json.loads(raw)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
        # 模型有时直接给单条
        if "what_kind_of_thing" in data or "title" in data:
            return [data]
    raise ValueError(f"无法从响应中解析 items：类型={type(data).__name__}")


def extract_chunk(
    client: OpenAI,
    chunk: Chunk,
    lines: list[str],
    stats: ExtractStats,
) -> list[dict[str, Any]]:
    user_content = format_chunk_for_prompt(chunk, lines)
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            t0 = time.perf_counter()
            response = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                temperature=TEMPERATURE,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
            )
            elapsed = time.perf_counter() - t0
            stats.calls += 1
            usage = response.usage
            if usage is not None:
                stats.prompt_tokens += usage.prompt_tokens or 0
                stats.completion_tokens += usage.completion_tokens or 0
                stats.total_tokens += usage.total_tokens or 0
            content = response.choices[0].message.content or ""
            items = _parse_items_payload(content)
            # 规范化 + 保留 extra
            normalized: list[dict[str, Any]] = []
            known = {
                "id",
                "title",
                "what_kind_of_thing",
                "summary",
                "audience",
                "line_start",
                "line_end",
            }
            for item in items:
                row: dict[str, Any] = {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "what_kind_of_thing": item.get("what_kind_of_thing"),
                    "summary": item.get("summary"),
                    "audience": item.get("audience"),
                    "line_start": item.get("line_start"),
                    "line_end": item.get("line_end"),
                    "chunk_index": chunk.index,
                }
                extra = {k: v for k, v in item.items() if k not in known}
                if extra:
                    row["extra"] = extra
                normalized.append(row)
            print(
                f"  chunk {chunk.index}: lines {chunk.line_start}-{chunk.line_end}, "
                f"{chunk.char_count} chars → {len(normalized)} items "
                f"({elapsed:.1f}s, attempt {attempt})",
                flush=True,
            )
            if attempt > 1:
                stats.retried_chunks.append(
                    {
                        "chunk_index": chunk.index,
                        "attempts": attempt,
                        "reason": str(last_err) if last_err else "retry succeeded",
                    }
                )
            return normalized
        except Exception as exc:  # noqa: BLE001 — 探针要记失败并重试
            last_err = exc
            print(
                f"  chunk {chunk.index}: attempt {attempt}/{MAX_RETRIES} failed: {exc}",
                flush=True,
            )
            if attempt < MAX_RETRIES:
                time.sleep(1.5 * attempt)
    stats.failed_chunks.append(
        {
            "chunk_index": chunk.index,
            "line_start": chunk.line_start,
            "line_end": chunk.line_end,
            "error": str(last_err),
        }
    )
    return []


def coverage_stats(total_lines: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    cover_count = [0] * (total_lines + 1)  # 1-based
    for item in items:
        raw_a = item.get("line_start")
        raw_b = item.get("line_end")
        if raw_a is None or raw_b is None:
            continue
        try:
            a = int(raw_a)
            b = int(raw_b)
        except (TypeError, ValueError):
            continue
        if a > b:
            a, b = b, a
        a = max(1, a)
        b = min(total_lines, b)
        for ln in range(a, b + 1):
            cover_count[ln] += 1

    covered = sum(1 for ln in range(1, total_lines + 1) if cover_count[ln] >= 1)
    overlap = sum(1 for ln in range(1, total_lines + 1) if cover_count[ln] >= 2)
    uncovered_ranges: list[tuple[int, int]] = []
    ln = 1
    while ln <= total_lines:
        if cover_count[ln] == 0:
            start = ln
            while ln <= total_lines and cover_count[ln] == 0:
                ln += 1
            uncovered_ranges.append((start, ln - 1))
        else:
            ln += 1
    pct = (covered / total_lines * 100.0) if total_lines else 0.0
    return {
        "total_lines": total_lines,
        "covered_lines": covered,
        "coverage_pct": pct,
        "overlap_lines": overlap,
        "uncovered_ranges": uncovered_ranges,
        "cover_count": cover_count,
    }


def _preview(lines: list[str], start: int, end: int, n: int = 30) -> str:
    """未覆盖区间开头 n 字——只写进 gitignored 报告，不进对话。"""
    parts: list[str] = []
    for ln in range(start, end + 1):
        parts.append(lines[ln - 1])
        if sum(len(p) for p in parts) >= n:
            break
    text = "".join(parts).replace("\n", " ")
    return text[:n]


def build_coverage_report(
    lines: list[str],
    items: list[dict[str, Any]],
    chunks: list[Chunk],
    stats: ExtractStats,
    cov: dict[str, Any],
) -> str:
    kind_counter: Counter[str] = Counter()
    audience_counter: Counter[str] = Counter()
    for item in items:
        kind = item.get("what_kind_of_thing")
        if kind is None or str(kind).strip() == "":
            kind_counter["(空)"] += 1
        else:
            kind_counter[str(kind).strip()] += 1
        aud = item.get("audience")
        if aud is None or str(aud).strip() == "":
            audience_counter["(空)"] += 1
        else:
            audience_counter[str(aud).strip()] += 1

    per_chunk: Counter[int] = Counter()
    for item in items:
        ci = item.get("chunk_index")
        if isinstance(ci, int):
            per_chunk[ci] += 1

    out: list[str] = []
    out.append("# 模组裸抽取 · 覆盖率报告")
    out.append("")
    out.append("## 覆盖率")
    out.append("")
    out.append(f"- 总行数：{cov['total_lines']}")
    out.append(f"- 被覆盖行数：{cov['covered_lines']}")
    out.append(f"- 覆盖率：{cov['coverage_pct']:.1f}%")
    out.append(f"- 被 ≥2 条条目同时覆盖的行数：{cov['overlap_lines']}")
    out.append("")
    out.append("### 未覆盖行号区间")
    out.append("")
    if not cov["uncovered_ranges"]:
        out.append("（无）")
    else:
        for a, b in cov["uncovered_ranges"]:
            preview = _preview(lines, a, b)
            if a == b:
                out.append(f"- 行 {a}：`{preview}`")
            else:
                out.append(f"- 行 {a}–{b}：`{preview}`")
    out.append("")
    out.append("## 汇总")
    out.append("")
    out.append(f"- 条目总数：{len(items)}")
    out.append(f"- LLM 调用次数：{stats.calls}")
    out.append(
        f"- token：prompt={stats.prompt_tokens}, "
        f"completion={stats.completion_tokens}, total={stats.total_tokens}"
    )
    out.append("")
    out.append("### what_kind_of_thing 去重清单")
    out.append("")
    for kind, count in kind_counter.most_common():
        out.append(f"- ({count}) {kind}")
    out.append("")
    out.append("### audience 取值分布")
    out.append("")
    for aud, count in audience_counter.most_common():
        out.append(f"- ({count}) {aud}")
    out.append("")
    out.append("### 每块条目数")
    out.append("")
    for chunk in chunks:
        out.append(
            f"- chunk {chunk.index}（行 {chunk.line_start}–{chunk.line_end}, "
            f"{chunk.char_count} 字）：{per_chunk.get(chunk.index, 0)} 条"
        )
    out.append("")
    if stats.failed_chunks:
        out.append("### 失败的块")
        out.append("")
        for fc in stats.failed_chunks:
            out.append(f"- chunk {fc['chunk_index']}: {fc['error']}")
        out.append("")
    if stats.retried_chunks:
        out.append("### 重试后成功的块")
        out.append("")
        for rc in stats.retried_chunks:
            out.append(
                f"- chunk {rc['chunk_index']}: {rc['attempts']} 次尝试 （{rc.get('reason', '')}）"
            )
        out.append("")
    return "\n".join(out) + "\n"


def default_outputs(input_path: Path) -> tuple[Path, Path]:
    stem = input_path.name
    base = stem[: -len(".txt")] if stem.endswith(".txt") else input_path.stem
    parent = input_path.parent
    return parent / f"{base}.裸抽取.json", parent / f"{base}.覆盖率.md"


def run(input_path: Path, json_out: Path, md_out: Path) -> int:
    print(f"input: {input_path}", flush=True)
    lines = read_numbered_lines(input_path)
    total_chars = sum(len(line) for line in lines) + max(0, len(lines) - 1)
    print(f"lines: {len(lines)}, chars≈{total_chars}", flush=True)

    chunks = build_chunks(lines, max_chars=MAX_CHUNK_CHARS)
    print(f"chunks: {len(chunks)}", flush=True)
    for c in chunks:
        print(
            f"  chunk {c.index}: lines {c.line_start}-{c.line_end}, {c.char_count} chars",
            flush=True,
        )

    api_key = load_api_key()
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=120.0)
    stats = ExtractStats()
    all_items: list[dict[str, Any]] = []

    t0 = time.perf_counter()
    for chunk in chunks:
        items = extract_chunk(client, chunk, lines, stats)
        all_items.extend(items)
    elapsed = time.perf_counter() - t0

    cov = coverage_stats(len(lines), all_items)
    report = build_coverage_report(lines, all_items, chunks, stats, cov)

    payload = {
        "source": str(input_path),
        "total_lines": len(lines),
        "chunk_count": len(chunks),
        "chunks": [
            {
                "index": c.index,
                "line_start": c.line_start,
                "line_end": c.line_end,
                "char_count": c.char_count,
            }
            for c in chunks
        ],
        "stats": {
            "elapsed_seconds": round(elapsed, 2),
            "calls": stats.calls,
            "prompt_tokens": stats.prompt_tokens,
            "completion_tokens": stats.completion_tokens,
            "total_tokens": stats.total_tokens,
            "failed_chunks": stats.failed_chunks,
            "retried_chunks": stats.retried_chunks,
        },
        "items": all_items,
    }

    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    md_out.write_text(report, encoding="utf-8")

    print(f"wrote: {json_out}", flush=True)
    print(f"wrote: {md_out}", flush=True)
    print(f"items: {len(all_items)}", flush=True)
    print(f"coverage: {cov['coverage_pct']:.1f}%", flush=True)
    print(f"elapsed: {elapsed:.1f}s, calls: {stats.calls}", flush=True)
    print(
        f"tokens: prompt={stats.prompt_tokens} "
        f"completion={stats.completion_tokens} total={stats.total_tokens}",
        flush=True,
    )
    if stats.failed_chunks:
        print(f"FAILED chunks: {stats.failed_chunks}", flush=True)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="模组裸抽取探针（契约发现）")
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="模组纯文本路径（.txt）",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="抽取 JSON 输出路径（默认：同目录 *.裸抽取.json）",
    )
    parser.add_argument(
        "--md-out",
        type=Path,
        default=None,
        help="覆盖率报告路径（默认：同目录 *.覆盖率.md）",
    )
    args = parser.parse_args(argv)
    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        print(f"输入文件不存在: {input_path}", file=sys.stderr)
        return 2
    json_out, md_out = default_outputs(input_path)
    if args.json_out is not None:
        json_out = args.json_out.expanduser().resolve()
    if args.md_out is not None:
        md_out = args.md_out.expanduser().resolve()
    return run(input_path, json_out, md_out)


if __name__ == "__main__":
    raise SystemExit(main())
