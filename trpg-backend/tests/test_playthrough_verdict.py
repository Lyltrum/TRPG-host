"""试跑判据本身要被验（`exec/29` 第 4 步落地记录之二）。

## 为什么这个函数值得一个测试文件

同一个验证器**已经坏过两次，两次都毫无声息**：

1. 第一版判据写成 `rooms.phase == "Finished"` —— 那个字符串在整个代码库里根本
   不存在，判据永远返回 False。
2. 第二版改对了字段，却预设「模组一定有可到达的结局」。林中屋的 `endings[]`
   只有一条 `epilogue`（源头是原文里的一行，取文层自己写着「关于模组尾声的
   叙述，提供战役延续的可能性」），而收束的唯一入口是 `ending_reached`。
   于是对这类模组，判据**结构上不可能被满足**——不管模组转得多好都报"没走到"。

两次的共同点：**验证器对某一类被测对象永远给同一个答案，而我会拿着那个答案去
怀疑被测对象。** 所以判据不能只有"必然通过"那一头的样本（追书人 4 条真结局），
必然失败那一头也得有——本文件就是那另一头。

## 🔴 顺序有语义

「卡住」要排在「没有及格线」前面。一局既卡住又没结局时，报 `open-ended` 就等于
拿一个良性事实把 bug 洗白了。`test_deadlock_wins_over_no_endings` 守这一条。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_DRIVER = Path(__file__).resolve().parents[2] / "e2e" / "scripts" / "module-playthrough.py"


def _load_driver():
    """文件名带连字符，import 不了，只能按路径加载。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    spec = importlib.util.spec_from_file_location("module_playthrough", _DRIVER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # 🔴 先登记再执行：`@dataclass` 会回查 `sys.modules[cls.__module__]`，
    # 不登记就在装饰 `TurnRecord` 时炸 AttributeError。
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


driver = _load_driver()


def _verdict(**kw) -> tuple[str, str]:
    base: dict = {
        "finished": False,
        "module_ending_ids": ["truth"],
        "silent_turns": 0,
        "turns_used": 10,
        "max_turns": 10,
        "driver_stalled": False,
    }
    base.update(kw)
    return driver._decide_verdict(**base)


# ── 两头都要有样本 ────────────────────────────────────


def test_reaching_an_ending_passes() -> None:
    """必然通过那一头：真的收束了。"""
    assert _verdict(finished=True)[0] == "ending"


def test_module_without_endings_is_not_reported_as_failure() -> None:
    """🔴 必然失败那一头：模组没有可到达的结局。

    旧判据在这里返回"没走到结局"，而那是**判据够不着**，不是模组坏了。
    """
    verdict, reason = _verdict(module_ending_ids=[])

    assert verdict == "open-ended"
    assert "没有可到达的结局" in reason


def test_module_with_endings_that_stalls_is_still_a_failure() -> None:
    """有及格线却没到 —— 这才是 ③「走得通」要抓的那件事，不能被一起放过。"""
    verdict, reason = _verdict(module_ending_ids=["asylum", "truth"])

    assert verdict == "stalled"
    assert "2 条结局" in reason


# ── 顺序 ──────────────────────────────────────────────


def test_deadlock_wins_over_no_endings() -> None:
    """🔴 既卡住又没结局时报"卡住"。反过来就是拿良性事实洗白 bug。"""
    assert _verdict(module_ending_ids=[], silent_turns=3)[0] == "broken"


def test_driver_stall_wins_over_no_endings() -> None:
    assert _verdict(module_ending_ids=[], driver_stalled=True)[0] == "broken"


def test_finished_wins_over_everything() -> None:
    """收束了就是收束了——中途卡过几轮不改变结论。"""
    assert _verdict(finished=True, silent_turns=5, module_ending_ids=[])[0] == "ending"


# ── 报告层不许出现没登记的结论 ──────────────────────


@pytest.mark.parametrize(
    "kw",
    [
        {"finished": True},
        {"module_ending_ids": []},
        {"module_ending_ids": ["a"]},
        {"silent_turns": 1},
        {"driver_stalled": True},
    ],
)
def test_every_verdict_is_registered(kw: dict) -> None:
    """`main()` 直接拿 verdict 去查 `VERDICTS` —— 漏登记就是 KeyError。"""
    assert _verdict(**kw)[0] in driver.VERDICTS


def test_no_verdict_comes_without_a_reason() -> None:
    """理由是给人看的那一半。空理由 = 报告里只剩一个孤零零的英文单词。"""
    assert all(_verdict(**kw)[1] for kw in ({"finished": True}, {"module_ending_ids": []}, {}))
