"""自修循环的形状（2026-08-20）。

## 🔴 为什么用 AST 守而不是跑一遍

这段循环要跑起来得有真 module + 真 report + 真 client，而它的缺陷恰恰是
**某条路径永远走不到**——那种缺陷用"跑一遍"证明不了（跑通的那条路本来就是
好的），只有把形状钉死才守得住。

真机实测的教训：2026-08-10 专门为 reach 做的窄路（每个悬空节点单独一次小
调用），在三轮自修里**一次都没被调用过**，日志里零个 `repair#` 标签。全套
测试、ruff、ty 全绿——没有任何机器检查会去问"这条分支到底走没走到"。
"""

from __future__ import annotations

import ast
import pathlib

_ASSEMBLE = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "module_probe" / "assemble.py"


def _repair_loop() -> ast.While:
    """自修那个 while 循环。"""
    tree = ast.parse(_ASSEMBLE.read_text(encoding="utf-8"))
    loops = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.While) and "MAX_REPAIR" in ast.dump(node.test)
    ]
    assert len(loops) == 1, f"预期只有一个自修循环，找到 {len(loops)} 个"
    return loops[0]


def test_the_regroup_branch_does_not_swallow_the_narrow_repairs() -> None:
    """🔴 **整份重吐之后不许无条件跳过窄路。**

    原来写的是：

        if report.ok or report.needs_stage1_repair():
            continue

    后半句意味着"还是归组问题就跳到下一轮"——于是只要存在任何一个
    secret_public / orphan / thin_slot / structure 错误，下面的技能归一、机械
    修补、leak 改写、reach 接线**一次都跑不到**。

    判据：**兜底的触发条件要包含「主路失败」，不能只包含「主路没走」。**

    **变异检验**：把 `needs_stage1_repair()` 加回那个 continue 的条件，
    这条当场红。
    """
    loop = _repair_loop()

    for node in ast.walk(loop):
        if not isinstance(node, ast.If):
            continue
        # 只看直接包着 continue 的 if
        if not any(isinstance(b, ast.Continue) for b in node.body):
            continue
        condition = ast.dump(node.test)
        assert "needs_stage1_repair" not in condition, (
            "continue 的条件里含 needs_stage1_repair ⇒ 归组类错误存在时，"
            "后面所有窄路都会被跳过（2026-08-10 那条 reach 窄路就是这么变成死代码的）"
        )


def test_the_loop_keeps_the_best_round_not_the_last() -> None:
    """🔴 自修每轮都可能重跑归组，而归组是概率的——**跑得越多不一定越好**。

    真机实测硬失败数是 3 → 2 → 6，而循环拒绝时交出去的是**最后一版**：手里
    明明有过一份只有 3 处问题的产物，被自己覆盖掉了。

    **变异检验**：删掉 `best_module = copy.deepcopy(module)` 那一行，这条当场红。
    """
    loop = _repair_loop()

    # 🔴 在**循环体内**找，不是全文 grep：循环外还有一处初始化的
    # `best_module = copy.deepcopy(module)`，全文匹配会被它蒙混过去
    # （第一版就是这么写的，变异体当场活了下来）。
    deep_copies = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "best_module" for t in node.targets)
        and isinstance(node.value, ast.Call)
        and "deepcopy" in ast.dump(node.value.func)
    ]
    assert deep_copies, (
        "循环体内没有对 best_module 的深拷贝——浅拷贝会跟着后面几轮一起被改掉，等于没留底"
    )


def test_the_bookkeeping_sits_at_the_top_of_the_loop() -> None:
    """🔴 **结算必须在轮首**：循环体里有好几处 `continue`，放轮尾会被跳过。

    这跟上面那条是同一个坑的两面——`continue` 吃掉轮尾的代码，正是 reach 窄路
    变成死代码的机制。

    **变异检验**：把留底那段移到循环体末尾，这条当场红。
    """
    loop = _repair_loop()
    body = loop.body

    # 轮首若干条语句里必须出现 best_ 的更新
    head = ast.dump(ast.Module(body=body[:3], type_ignores=[]))
    assert "best_module" in head, "留底不在轮首前三条语句里，会被 continue 跳过"


def test_it_stops_when_it_stops_improving() -> None:
    """没有收敛判据的循环会一直烧到上限，而实测它会越修越糟。"""
    loop = _repair_loop()

    # 🔴 断言那个 break 是**可达的**：`if False and stalled >= 2: break` 里
    # 既有 stalled 也有 break，光数它们存不存在，反例照样通过（第一版如此）。
    guards = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.If)
        and any(isinstance(b, ast.Break) for b in node.body)
        and "stalled" in ast.dump(node.test)
    ]
    assert guards, "没有以 stalled 为条件的提前退出"
    for guard in guards:
        dumped = ast.dump(guard.test)
        assert "Constant(value=False)" not in dumped, "退出条件被常量 False 短路了，等于没有"
        assert "Compare" in dumped, "退出条件不是一次真实比较"
