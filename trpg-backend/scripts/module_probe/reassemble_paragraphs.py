"""段落重组——消除 PDF 提取工具的「按视觉行切碎」伪影。

契约发现实验的输入预处理。只做换行合并，绝不清洗、去重、改写或删段。

设计判断：
- PDF/OCR 提取常按视觉行断开段落；这是提取工具的伪影，不是模组本身的形状。
- 碎行会让裸抽取探针「按结构边界分块」失效，模型也只看到碎片。
- 消除伪影 ≠ 加工内容：只改换行位置，字符序列在去掉空白后必须完全不变。
- 无损校验是硬门槛：compact(原文) == compact(重组后)，否则报错退出。

用法（在 trpg-backend/ 下）：

    .venv/bin/python scripts/module_probe/reassemble_paragraphs.py \\
        --input ../模组资料/某模组.txt

    # 强制重组 / 强制不重组 / 只检查
    .venv/bin/python scripts/module_probe/reassemble_paragraphs.py \\
        --input ../模组资料/某模组.txt --force
    .venv/bin/python scripts/module_probe/reassemble_paragraphs.py \\
        --input ../模组资料/某模组.txt --no-reassemble --check-only

自动判定：非空行平均行长 < 阈值（默认 40）则重组。
产物默认：需要重组时写 *.重组.txt；不需要时不写文件（仍打印校验结果）。
脚本只吃路径参数，不内嵌任何模组正文。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 行尾终止标点：遇到则本段可收束，不再与下一行合并。
# 含中英文常见句读与部分闭合括号/引号。
TERMINAL_PUNCT = frozenset("。！？：；…》」』）】.!?:;)]")

# 非空行平均行长低于此值 → 视为「被按视觉行切碎」，需要重组。
DEFAULT_AVG_LINE_THRESHOLD = 40.0
# 短行且无终止标点、且当前无拼接缓冲 → 视为标题，独立成行。
SHORT_LINE_MAX = 20


def compact_text(text: str) -> str:
    """去掉所有空白字符后的文本——无损校验的指纹。"""
    return re.sub(r"\s+", "", text, flags=re.UNICODE)


def ends_with_terminal(line: str) -> bool:
    """判断行尾（忽略尾部空白）是否为终止标点。"""
    s = line.rstrip()
    if not s:
        return False
    return s[-1] in TERMINAL_PUNCT


def line_stats(lines: list[str]) -> dict[str, float | int]:
    nonempty = [ln for ln in lines if ln.strip()]
    n = len(lines)
    nn = len(nonempty)
    avg_all = (sum(len(ln) for ln in lines) / n) if n else 0.0
    avg_ne = (sum(len(ln) for ln in nonempty) / nn) if nn else 0.0
    return {
        "total_lines": n,
        "nonempty_lines": nn,
        "avg_line_len": avg_all,
        "avg_nonempty_line_len": avg_ne,
    }


def reassemble_lines(lines: list[str]) -> list[str]:
    """按规则合并碎行，返回重组后的行列表。

    规则（守住无损：只决定何处插入换行，不增删非空白字符）：
    1. 空行（strip 后为空）保留为段落分隔；
    2. 行尾不是终止标点 → 与下一行拼接（直接相连，不加额外字符）；
    3. 短行（≤ SHORT_LINE_MAX 字）且不以终止标点结尾、
       且当前没有正在拼接的缓冲 → 视为标题，独立成行。
    """
    out: list[str] = []
    buf: str | None = None

    def flush() -> None:
        nonlocal buf
        if buf is not None:
            out.append(buf)
            buf = None

    for line in lines:
        if not line.strip():
            flush()
            out.append("")
            continue

        if buf is None:
            short = len(line.strip()) <= SHORT_LINE_MAX
            terminal = ends_with_terminal(line)
            if short and not terminal:
                # 标题：独立成行
                out.append(line)
            elif terminal:
                out.append(line)
            else:
                buf = line
        else:
            # 正在拼接：只做字符串相连（消除换行伪影）
            buf = buf + line
            if ends_with_terminal(line):
                flush()

    flush()
    return out


def reassemble_text(text: str) -> str:
    """重组整份文本；保留原文是否以换行结尾的形态。"""
    lines = text.splitlines()
    new_lines = reassemble_lines(lines)
    body = "\n".join(new_lines)
    if text.endswith("\n") and (not body.endswith("\n") if body else True):
        body = body + "\n" if body else "\n"
    elif not text.endswith("\n") and body.endswith("\n") and body != "\n":
        body = body.rstrip("\n")
    return body


def assert_lossless(original: str, reassembled: str) -> None:
    """硬门槛：去掉所有空白后字符串必须完全相等。"""
    a = compact_text(original)
    b = compact_text(reassembled)
    if a != b:
        # 定位首个差异，便于诊断（不打印模组正文大段）
        limit = min(len(a), len(b))
        pos = next((i for i in range(limit) if a[i] != b[i]), limit)
        orig_ch = repr(a[pos]) if pos < len(a) else "EOF"
        new_ch = repr(b[pos]) if pos < len(b) else "EOF"
        raise SystemExit(
            f"无损校验失败：compact 长度 orig={len(a)} new={len(b)}，"
            f"首差位置={pos}（orig_ch={orig_ch} new_ch={new_ch}）。"
            f"拒绝继续，请检查重组算法。"
        )


def default_output_path(input_path: Path) -> Path:
    stem = input_path.name
    base = stem[: -len(".txt")] if stem.endswith(".txt") else input_path.stem
    return input_path.parent / f"{base}.重组.txt"


def run(
    input_path: Path,
    output_path: Path | None,
    *,
    force: bool,
    no_reassemble: bool,
    check_only: bool,
    threshold: float,
) -> int:
    print(f"input: {input_path}", flush=True)
    original = input_path.read_text(encoding="utf-8")
    lines = original.splitlines()
    st = line_stats(lines)
    print(
        f"lines: {st['total_lines']} (nonempty={st['nonempty_lines']}), "
        f"avg_all={st['avg_line_len']:.1f}, "
        f"avg_nonempty={st['avg_nonempty_line_len']:.1f}",
        flush=True,
    )
    compact_len = len(compact_text(original))
    print(f"compact_chars: {compact_len}", flush=True)

    if no_reassemble:
        do_reassemble = False
        reason = "--no-reassemble"
    elif force:
        do_reassemble = True
        reason = "--force"
    else:
        avg_ne = float(st["avg_nonempty_line_len"])
        do_reassemble = avg_ne < threshold
        reason = (
            f"auto: avg_nonempty={avg_ne:.1f} "
            f"{'<' if do_reassemble else '>='} threshold={threshold}"
        )

    print(f"decision: reassemble={do_reassemble} ({reason})", flush=True)

    result = reassemble_text(original) if do_reassemble else original

    # 无论是否重组，都跑无损校验（不重组时是恒等；重组时是硬门槛）
    assert_lossless(original, result)
    print("lossless_check: PASS (compact text identical)", flush=True)

    new_lines = result.splitlines()
    st2 = line_stats(new_lines)
    print(
        f"after: lines={st2['total_lines']} (nonempty={st2['nonempty_lines']}), "
        f"avg_all={st2['avg_line_len']:.1f}, "
        f"avg_nonempty={st2['avg_nonempty_line_len']:.1f}",
        flush=True,
    )
    print(
        f"line_delta: {st['total_lines']} → {st2['total_lines']} "
        f"({int(st2['total_lines']) - int(st['total_lines']):+d})",
        flush=True,
    )

    if check_only:
        print("check_only: not writing output", flush=True)
        return 0

    if not do_reassemble:
        # 不需要重组时默认不写文件；若用户显式给了 --output 则写出副本
        if output_path is not None:
            out = output_path
            out.write_text(result, encoding="utf-8")
            print(f"wrote (identity copy): {out}", flush=True)
        else:
            print("no reassembly needed: skip write (use original as probe input)", flush=True)
        return 0

    out = output_path if output_path is not None else default_output_path(input_path)
    out.write_text(result, encoding="utf-8")
    print(f"wrote: {out}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="段落重组：消除 PDF 视觉行切碎伪影（无损）",
    )
    parser.add_argument("--input", required=True, type=Path, help="模组纯文本路径")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出路径（默认：需要重组时写 *.重组.txt）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重组（忽略平均行长判定）",
    )
    parser.add_argument(
        "--no-reassemble",
        action="store_true",
        help="强制不重组，只做无损恒等校验",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只跑判定与无损校验，不写文件",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_AVG_LINE_THRESHOLD,
        help=f"自动重组阈值：非空行平均行长 < 此值则重组（默认 {DEFAULT_AVG_LINE_THRESHOLD}）",
    )
    args = parser.parse_args(argv)
    if args.force and args.no_reassemble:
        print("不能同时指定 --force 与 --no-reassemble", file=sys.stderr)
        return 2
    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        print(f"输入文件不存在: {input_path}", file=sys.stderr)
        return 2
    output_path = args.output.expanduser().resolve() if args.output else None
    return run(
        input_path,
        output_path,
        force=args.force,
        no_reassemble=args.no_reassemble,
        check_only=args.check_only,
        threshold=args.threshold,
    )


if __name__ == "__main__":
    raise SystemExit(main())
