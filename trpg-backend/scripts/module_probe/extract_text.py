"""取文 CLI：任意文件 → `*.txt`（`exec/29` 第 2 步）。

**薄委托**——真正的实现在 `app/core/module_import/extract.py`，因为它要被
service 层调用（第 3/5 步）。这里只负责命令行参数与落盘。
判据见 CLAUDE.md：**逻辑搬走，接缝留下**。

用法（在 trpg-backend/ 下）：

    .venv/bin/python scripts/module_probe/extract_text.py --input ../模组资料/某模组.pdf

产物写到输入文件同目录的 `*.txt`（gitignored）。
接着通常要跑 `reassemble_paragraphs.py`（PDF 抽出来会被按视觉行切碎）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_BACKEND_ROOT = next(p for p in Path(__file__).resolve().parents if p.name == "trpg-backend")
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.core.module_import.extract import (  # noqa: E402
    UnsupportedDocumentError,
    extract_document,
)


def main() -> None:
    ap = argparse.ArgumentParser(description="模组取文：任意文件 → 纯文本")
    ap.add_argument("--input", type=Path, required=True, help="模组原始文件（pdf/docx/txt）")
    ap.add_argument("--output", type=Path, default=None, help="默认写到输入同目录的 .txt")
    args = ap.parse_args()

    try:
        doc = extract_document(args.input)
    except UnsupportedDocumentError as exc:
        raise SystemExit(f"取文失败：{exc}") from exc

    out = args.output or args.input.with_suffix(".txt")
    out.write_text(doc.text, encoding="utf-8")

    print(f"input:   {args.input}")
    print(f"format:  {doc.format}  页数 {doc.page_count}  图片 {doc.image_count}")
    print(f"chars:   {len(doc.text)}")
    for w in doc.warnings:
        print(f"⚠️  {w}")
    print(f"wrote:   {out}")


if __name__ == "__main__":
    main()
