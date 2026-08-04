"""转换管线的编排约束（`exec/29` 第 3 步）。

整条链要发几十次 LLM 调用，没法在单元测试里跑完。这里只守**不需要模型就能定的
那几条**——而它们恰好是最容易退化的：

- 🔴 **不可用的输入必须在烧钱之前退出。** 取文失败却继续往下走，等于为一份读不了
  的文件付了整条管线的钱，最后再告诉用户"不行"。
- 🔴 **策略是固定的，不是"看情况"。** 实测四份模组里只有两份有 `-分批` 产物，
  那是当年人看情况定的痕迹；导入功能里没有那个人。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.module_probe.pipeline import (  # noqa: E402
    RELATION_BATCH_SIZE,
    ConversionError,
    convert,
)


def test_unreadable_input_fails_before_any_llm_call(tmp_path: Path) -> None:
    """🔴 取文失败要当场退出——不能为一份读不了的文件付整条管线的钱。

    这里用的是一份 `.doc` 壳子（OLE2 魔数但不是 Word 文档）：取文层会拒绝它，
    而拒绝必须发生在裸抽取之前。没有网络也应当能跑过这条用例。
    """
    src = tmp_path / "broken.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0 not a word document")

    with pytest.raises(ConversionError) as exc:
        convert(src, work_dir=tmp_path / "work", out_structured=tmp_path / "out.json")

    assert ".docx" in str(exc.value), "拒绝理由要带可执行的下一步"


def test_unknown_format_fails_before_any_llm_call(tmp_path: Path) -> None:
    src = tmp_path / "m.epub"
    src.write_bytes(b"whatever")

    with pytest.raises(ConversionError):
        convert(src, work_dir=tmp_path / "work", out_structured=tmp_path / "out.json")


def test_batching_is_a_fixed_policy_not_a_judgement_call() -> None:
    """关系发现**总是分批**。

    分批是全量的超集，只是慢；导入是后台任务，不在乎慢。把它做成常量而不是参数，
    是为了不让"看情况"这个动作重新长回来。
    """
    assert isinstance(RELATION_BATCH_SIZE, int)
    assert RELATION_BATCH_SIZE > 0
