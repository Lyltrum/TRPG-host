"""对抗结论必须进历史窗口（2026-08-18 真机，同一个 bug 的第二、三次）。

## 🔴 根因不是模型不遵守，是结论没送到

两跑写反的形态一模一样：**玩家自己成功、对手成功等级更高 ⇒ 判负**，叙事写成
玩家赢。`exec/20 §1.20` 当时判成"prompt 手段已用尽，只能靠状态化硬化"——那是
**拿错了度量对象**：量的是"代码组装了什么"，不是"叙事真正收到了什么"。

真相是两件事叠在一起：

1. `history.py` 渲染 `keeper.check` 时只取玩家自己那一半（`rolled`/`target`/
   `level`），`opposed` 整块**包括 `verdict` 一个字都没进去**；
2. `settle_skill_check` 里那段结论提句首、明写"不要按成功等级自行推断"的三态
   文本，写进的是 `deps.check_results`——**那个字段从头到尾没有任何读取方**。

⇒ 结算叙事那一拍，模型眼里只有「柯文进行了一次力量检定，掷出34，目标60，
结果**成功**」。它没有输给谁这回事可以违背。
"""

from __future__ import annotations

from app.core.keeper.memory.history import _render_check
from app.core.keeper.primitives import dice

#: 真机那一拍对抗那一半的原样内容（人名换成中性称呼，模组专名不进 git）。
_THE_REAL_OPPOSED: dict = {
    "opponent": "那个要跑的人",
    "rolled": 10,
    "target": 50,
    "level": "极难成功",
    "won": False,
    "verdict": dice.VERDICT_LOSE,
}

#: 真机那一拍的原样 payload。
_THE_REAL_ONE: dict = {
    "player": "柯文",
    "skill": "力量",
    "rolled": 34,
    "target": 60,
    "level": "成功",
    "opposed": _THE_REAL_OPPOSED,
}


def test_the_verdict_reaches_the_narrator() -> None:
    """🔴 **变异检验**：把 `_render_check` 里那段 `if isinstance(opposed, dict)`
    去掉，这条当场红——回到只渲染玩家自己那一半的老样子。"""
    line = _render_check(_THE_REAL_ONE)
    assert "输给" in line
    assert "那个要跑的人" in line


def test_the_misleading_half_never_reaches_the_narrator() -> None:
    """🔴 **只给结论，不给两边的出目与成功等级**。

    「保密靠拿不到，不是请你别说」：玩家那半句「结果**成功**」正是两跑的误导源，
    结论提句首、明写反向指令都没能盖过它。数字对后续叙事没有价值（复述骰子
    本来就不该），而玩家侧的可见性由 `check.result` 那条 WS 事件保证——
    **两件事分属两个出口，不要混。**

    **变异检验**：在对抗那一支里把出目或 `level` 拼回去，这条当场红。
    """
    line = _render_check(_THE_REAL_ONE)
    assert "34" not in line and "60" not in line
    assert "10" not in line and "50" not in line
    assert "成功" not in line, f"玩家自己那半句「成功」不能出现在对抗行里：{line}"


def test_all_three_verdicts_render() -> None:
    """三态各有各的说法。**僵持不许写成任何一方失败**——顿足舞那一跑判了僵持，
    叙事写成「你被那股力气猛地甩开」，读起来是单方面输。"""
    seen = set()
    for verdict in (dice.VERDICT_WIN, dice.VERDICT_LOSE, dice.VERDICT_STALEMATE):
        payload = {**_THE_REAL_ONE, "opposed": {**_THE_REAL_OPPOSED, "verdict": verdict}}
        line = _render_check(payload)
        assert line not in seen, f"{verdict} 跟别的三态撞了同一句话"
        seen.add(line)
    stalemate = _render_check(
        {
            **_THE_REAL_ONE,
            "opposed": {**_THE_REAL_OPPOSED, "verdict": dice.VERDICT_STALEMATE},
        }
    )
    assert "僵持" in stalemate and "谁都没得手" in stalemate
    assert "输" not in stalemate and "赢" not in stalemate


def test_a_plain_check_is_unchanged() -> None:
    """非对抗检定一个字都不变——那条路上出目本来就是叙事该知道的。"""
    line = _render_check(
        {"player": "柯文", "skill": "侦察", "rolled": 11, "target": 47, "level": "困难成功"}
    )
    assert line == "柯文进行了一次侦察检定，掷出11，目标47，结果困难成功。"


def test_a_broken_opposed_block_falls_back_instead_of_crashing() -> None:
    """脏数据退回普通检定那一支，不炸掉整段历史——历史窗口每轮都要渲染。"""
    for bad in ({"verdict": "看不懂的值"}, {}, "不是字典", None):
        line = _render_check({**_THE_REAL_ONE, "opposed": bad})
        assert "柯文" in line
