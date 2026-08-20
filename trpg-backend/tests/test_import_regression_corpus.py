"""导入管线的离线回归集（2026-08-20）。

## 🔴 为什么必须有它

在这之前，验证这条管线的唯一手段是**跑一次真模组**：20 分钟、一次钱、只覆盖
**一份**模组的**一条随机路径**（LLM 每次结果不同）。于是修法只能是"跑一次 →
撞到哪修哪"，而那正是项目 CLAUDE.md 里记了好几次的那条错误——**对着一个样本
调判据，还当它是通例**。

真机证据：连着三轮跑坨子岛，每轮报出来的问题节点名都不一样
（`third-day-trap-su` → `dup-ticket-booth` / `third-day-trap`），修的是现象
不是根因。

判据：**手上有几份同类数据就全量跑一遍。**

## 它测什么

`模组资料/` 里每一份**已经导成功、已经用来跑过真机**的模组，它们的中间产物
（裸抽取 / 组装中间态 / structured / 源文本）都还在。管线改一行，就在这几份上
重放**校验**——纯代码、秒级、免费、全部一起跑。

守的是：**改管线不许把已经能用的模组弄坏。**

🔴 它第一次跑就抓到了一个真回归：受众翻译层上线当天，老产物里没有
`audience_kind`，而 `_audience_is_keeper_secret` 的兜底「没有 kind 就当 kp」
把神秘渡轮的两个公开片段判成了绝密。

## 边界

- 只重放**校验**，不重放 LLM 那几步（那要磁带，见 `test_module_import_tape`）
- 第三方模组正文是 gitignored 的，CI 上没有 ⇒ 缺数据就 skip，同磁带那条
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_CORPUS = _BACKEND.parent / "模组资料"

#: 🔴 **每一份的期望是它当年进库时的状态，不是"零错误"**。
#:
#: 复足与神秘渡轮当年就带着 trace 问题被导进去了，而它们跑过好几局真机，
#: 从没出过溯源相关的问题——那是「trace 该降级而不是拒绝」最硬的证据。
#: 把期望写成 0 会逼着后来的人去"修"两个实际没坏的东西。
_EXPECTED_HARD: dict[str, int] = {
    "林中屋": 0,
    "死者的顿足舞": 0,
    "科比特先生": 0,
    "复足": 2,  # 全是 trace
    "神秘渡轮": 2,  # 全是 trace
}


def _load(name: str) -> dict[str, Any] | None:
    """一份模组的四件产物。缺任何一件就返回 None。"""
    try:
        extract = next(_CORPUS.glob(f"{name}*.裸抽取.json"))
        intermediate = next(_CORPUS.glob(f"{name}*.组装中间态.json"))
        structured = next(_CORPUS.glob(f"{name}*structured.json"))
        source = next(f for f in _CORPUS.glob(f"{name}*.txt") if "重组" not in f.name)
    except StopIteration:
        return None

    items = json.loads(extract.read_text(encoding="utf-8"))["items"]
    stage1 = json.loads(intermediate.read_text(encoding="utf-8"))["stage1"]
    # assignment_map 是运行时构造的，中间态里存的是 assignments 列表
    amap = {
        a["item_id"]: {k: v for k, v in a.items() if k != "item_id"} for a in stage1["assignments"]
    }
    return {
        "items": items,
        "assignment_map": amap,
        "module": json.loads(structured.read_text(encoding="utf-8")),
        "source": source,
    }


def _backfill_audience_kinds(items: list[dict[str, Any]]) -> None:
    """给老产物补上 `audience_kind`。

    🔴 **这一步在生产里由受众翻译层（LLM）做**，而这些产物是翻译层上线之前
    产生的。装置必须补上它，否则测的是"缺字段时的兜底行为"，不是这份模组当年
    真实的样子——第一次跑就是这么误报了神秘渡轮两条 secret_public。

    这里用的是**当年那版关键词判据**，因为要重现的正是这些模组当年进库时的
    状态。新模组走翻译层，不走这里。
    """
    for it in items:
        if it.get("audience_kind"):
            continue
        audience = str(it.get("audience") or "")
        secret = any(sig in audience for sig in ("绝密", "守密人", "守秘人", "KP", "kp"))
        it["audience_kind"] = "kp" if secret else "player"


@pytest.mark.parametrize("name", sorted(_EXPECTED_HARD))
def test_an_already_working_module_still_validates(name: str) -> None:
    """🔴 **改管线不许把已经能用的模组弄坏。**

    每一份都是真人跑过局的模组。硬失败数变多 = 我刚引入了一个回归；变少也要
    看一眼——那可能是好事（修法生效），也可能是校验被削弱了。
    """
    sys.path.insert(0, str(_BACKEND / "scripts" / "module_probe"))
    data = _load(name)
    if data is None:
        pytest.skip(
            f"{name} 的中间产物不全（第三方正文 gitignored，CI 上没有）"  # ty: ignore[too-many-positional-arguments]
        )

    from scripts.module_probe.probe import read_numbered_lines  # noqa: PLC0415
    from scripts.module_probe.validate_module import validate_assembled  # noqa: PLC0415

    _backfill_audience_kinds(data["items"])
    report = validate_assembled(
        data["module"],
        source_item_ids={str(i["id"]) for i in data["items"] if i.get("id")},
        assignment_map=data["assignment_map"],
        items=data["items"],
        source_lines=read_numbered_lines(data["source"]),
    )

    hard = report.all_errors()
    expected = _EXPECTED_HARD[name]
    assert len(hard) == expected, (
        f"{name} 的硬失败数从 {expected} 变成 {len(hard)}：\n"
        + "\n".join(f"  {e}" for e in hard[:10])
    )


def test_the_corpus_is_not_silently_empty() -> None:
    """🔴 **装置自证**：全都 skip 掉的回归集等于没有回归集。

    本机有这些产物，所以这里断言至少加载得到一份。CI 上整个文件都会 skip
    （包括这一条），那是有意的——第三方正文进不了 git。
    """
    if not _CORPUS.exists():
        pytest.skip("本机没有 模组资料/")  # ty: ignore[too-many-positional-arguments]
    loaded = [name for name in _EXPECTED_HARD if _load(name) is not None]
    assert loaded, "模组资料/ 在，却一份产物都加载不到——glob 模式可能坏了"
