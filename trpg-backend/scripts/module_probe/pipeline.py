"""转换管线：一条命令，原始文件 → structured（`exec/29` 第 3 步）。

在这之前这条链是**人工串的五步**，而且中间有两处是人在看情况做判断
（分不分批、要不要重组）。实测四份模组里只有两份有 `-分批` 产物——那就是
「人当时看情况定的」留下的痕迹。**导入功能里没有那个人**，所以这里把策略固定下来。

    取文 → 段落重组 → 裸抽取 → 关系发现(分批×2) → 组装+校验+自修

## 固定的策略（不再看情况）

| 决定 | 固定成什么 | 理由 |
|---|---|---|
| 分批 | **总是分批** | 分批是全量的超集，只是慢；导入是后台任务，不在乎慢 |
| 重组 | **按脚本自己的阈值自动判**（带无损校验） | 那个判据已经有了，且校验硬 |
| 自修 | 上限由 `assemble` 内部控制 | 超出仍不过 → **显式失败**，不产出半份 |

## 🔴 中间产物落工作目录，不落 `模组资料/`

以前脚本把 `*.裸抽取.json` 等一堆中间态写在原始文件旁边，那是**开发期的**做法。
导入功能面对的是用户上传的文件，产物要能整批清理，所以走 `--work-dir`
（默认临时目录）。**版权红线照旧**：中间产物含第三方正文，绝不进 git。

## LLM 客户端已统一（2026-08-04）

三个脚本原本各自 `OpenAI(api_key=…)`，导入过程录不了也放不了。现在都走
`build_sync_llm_client`（`app/core/llm_tape.py` 的同步那一半），三个 `tape_kind`：

    probe → module_probe   relation_probe → module_relations   assemble → module_assemble

录一条：

    LLM_TAPE_MODE=record LLM_TAPE_PATH=tapes/林中屋.json \\
    LLM_TAPE_SCENARIO=模组资料/林中屋.pdf .venv/bin/python scripts/module_probe/pipeline.py ...

🔴 **磁带含模组正文（prompt 是整份原文，响应是 structured 产物），只能落
gitignored 的 `trpg-backend/tapes/`**，跟 keeper 的真实模组磁带同一条红线。
所以它是**本地回归工具，进不了 CI**——回归用例没有磁带时 skip。

🔴 回放时 `response.usage` 是 `None`（磁带没录 token 数），统计出来的成本会是
0。三个脚本本来就写着 `if usage is not None`，行为不变。

## 用法

    cd trpg-backend && .venv/bin/python scripts/module_probe/pipeline.py \\
        --input ../模组资料/某模组.pdf --out /tmp/某模组.structured.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

_BACKEND_ROOT = next(p for p in Path(__file__).resolve().parents if p.name == "trpg-backend")
for _p in (str(_BACKEND_ROOT), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import assemble  # noqa: E402
import probe  # noqa: E402
import reassemble_paragraphs  # noqa: E402
import relation_probe  # noqa: E402

from app.core.llm_tape import activate_from_env  # noqa: E402
from app.core.module_import.extract import (  # noqa: E402
    UnsupportedDocumentError,
    extract_document,
)

#: 关系发现每批的焦点条目数。批越小越稳、调用越多。
RELATION_BATCH_SIZE = 15


class ConversionError(RuntimeError):
    """转换失败。**必须带上人能看懂的原因**——它会变成给用户的拒绝理由。"""


@dataclass
class ConversionResult:
    ok: bool
    source_format: str = ""
    page_count: int = 0
    image_count: int = 0
    chars: int = 0
    items: int = 0
    reassembled: bool = False
    structured_path: str = ""
    report_path: str = ""
    hard_failures: int = 0
    failure_reason: str = ""
    elapsed_s: float = 0.0
    warnings: list[str] = field(default_factory=list)


def convert(
    source: Path,
    *,
    work_dir: Path,
    out_structured: Path,
    on_stage: Callable[[str], None] | None = None,
) -> ConversionResult:
    """跑完整条链。任何一步失败都**抛 `ConversionError`**，不返回半份产物。

    `on_stage`：每进入一个阶段调一次，取值是 `job_state.STAGES` 里的词。
    导入 job 拿它报进度——**分阶段是因为每一阶段的失败对用户意味着不同的下一步**，
    不是为了好看。CLI 不传，行为不变。
    """
    t0 = time.perf_counter()
    notify = on_stage or (lambda _stage: None)
    work_dir.mkdir(parents=True, exist_ok=True)
    stem = source.stem
    result = ConversionResult(ok=False)

    # ── 1. 取文 ────────────────────────────────────
    notify("extracting")
    try:
        doc = extract_document(source)
    except UnsupportedDocumentError as exc:
        raise ConversionError(str(exc)) from exc
    result.source_format = doc.format
    result.page_count = doc.page_count
    result.image_count = doc.image_count
    result.warnings.extend(doc.warnings)

    raw_txt = work_dir / f"{stem}.txt"
    raw_txt.write_text(doc.text, encoding="utf-8")
    print(f"[1/5] 取文 {doc.format} 页{doc.page_count} 图{doc.image_count} 字{len(doc.text)}")

    # ── 2. 段落重组（PDF 会被按视觉行切碎；脚本自带无损校验）──
    source_txt = raw_txt
    if reassemble_paragraphs.main(["--input", str(raw_txt)]) != 0:
        raise ConversionError("段落重组失败（无损校验没过，文本可能已损坏）")
    reassembled = raw_txt.with_name(f"{raw_txt.stem}.重组.txt")
    if reassembled.exists():
        source_txt = reassembled
        result.reassembled = True
    result.chars = len(source_txt.read_text(encoding="utf-8"))
    print(f"[2/5] 重组 {'已重组' if result.reassembled else '无需重组'} 字{result.chars}")

    # ── 3. 裸抽取 ──────────────────────────────────
    notify("probing")
    extract_json = work_dir / f"{stem}.裸抽取.json"
    coverage_md = work_dir / f"{stem}.覆盖率.md"
    if probe.run(source_txt, extract_json, coverage_md) != 0:
        raise ConversionError("裸抽取失败")
    result.items = len(json.loads(extract_json.read_text(encoding="utf-8"))["items"])
    print(f"[3/5] 裸抽取 {result.items} 条")
    if result.items == 0:
        raise ConversionError("这份文稿抽不出任何条目，可能不是模组文本")

    # ── 4. 关系发现（🔴 总是分批）────────────────────
    notify("relating")
    if relation_probe.run(extract_json, source_txt, batch_size=RELATION_BATCH_SIZE) != 0:
        raise ConversionError("关系发现失败")
    pass1 = work_dir / f"{stem}.关系-pass1.json"
    pass2 = work_dir / f"{stem}.关系-pass2.json"
    if not (pass1.exists() and pass2.exists()):
        raise ConversionError("关系发现没有产出两遍对拍数据")
    print("[4/5] 关系发现 完成（分批）")

    # ── 5. 组装 + 校验 + 自修 ───────────────────────
    notify("assembling")
    intermediate = work_dir / f"{stem}.组装中间态.json"
    report = work_dir / f"{stem}.校验报告.json"
    assemble.run_pipeline(
        extract_path=extract_json,
        pass1_path=pass1,
        pass2_path=pass2,
        source_txt=source_txt,
        example_path=None,
        out_structured=out_structured,
        out_intermediate=intermediate,
        out_report=report,
    )
    result.structured_path = str(out_structured)
    result.report_path = str(report)

    payload = json.loads(report.read_text(encoding="utf-8"))
    # 🔴 显式降级，不是静默丢弃：模组附的预设调查员卡本系统用不上（角色由玩家
    # 自己建），但丢了多少必须说出来——跟图片占位同一条纪律。
    pregen = (payload.get("out_of_scope_counts") or {}).get("pregen", 0)
    if pregen:
        # 🔴 说"片段"不说"张"：我们数的是归到 pregen 的**片段**，而一张卡常被
        # 切成好几段（标题/属性/背景/页码各一段），实测 4 张卡数出 14 段。
        # 报一个自己算不出来的单位，就是在编。
        result.warnings.append(
            f"这份模组附了预设调查员卡（{pregen} 段材料）。本系统的角色由玩家自己建，"
            "这部分没有收进来。"
        )
    errors = payload.get("report", {}).get("all_errors") or []
    result.hard_failures = len(errors)
    result.ok = bool(payload.get("success")) and not errors
    print(f"[5/5] 组装+校验 硬失败 {result.hard_failures} 条")

    result.elapsed_s = round(time.perf_counter() - t0, 1)
    if not result.ok:
        # 🔴 拒绝理由受剧透约束：只说数量与类别，不带 id、不带正文（`exec/29 §2`）
        kinds = sorted({e.split("]")[0].lstrip("[") for e in errors if "]" in e})
        result.failure_reason = f"校验未通过：{result.hard_failures} 处问题（{'、'.join(kinds)}）"
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="模组转换管线：原始文件 → structured")
    ap.add_argument("--input", type=Path, required=True, help="模组原始文件（pdf/docx/doc/txt）")
    ap.add_argument("--out", type=Path, required=True, help="structured JSON 输出路径")
    ap.add_argument("--work-dir", type=Path, default=None, help="中间产物目录（默认临时目录）")
    ap.add_argument("--keep-work", action="store_true", help="保留中间产物（默认删）")
    args = ap.parse_args(argv)

    # 🔴 CLI 必须自己开磁带。`activate_from_env` 原先只在 `app/main.py` 的
    # lifespan 里被调用——服务端那条路有人开，**命令行这条没有**。于是
    # `LLM_TAPE_MODE=record` 在这里是个静默空操作：跑完 ¥0.35，一条都没录上，
    # 而且什么都不会提示。同族于「探测器不是闸门，零命中 ≠ 没问题」。
    session = activate_from_env()

    tmp = args.work_dir or Path(tempfile.mkdtemp(prefix="module-import-"))
    try:
        result = convert(args.input, work_dir=tmp, out_structured=args.out)
    except ConversionError as exc:
        print(f"\n转换失败：{exc}")
        return 1
    finally:
        if args.work_dir is None and not args.keep_work and tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    if session is not None:
        # 🔴 **把录到的条数说出来。** 空磁带跟没开录制看起来一模一样，而两者
        # 差着一次真金白银的运行——不报数字就没人会发现。
        session.flush()
        print(f"\n磁带：{session.path} · {len(session.tape.entries)} 条")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
