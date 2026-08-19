"""规则 4e-2：不打算让玩家去的时候，不许直接宣告「你去不了」（2026-08-18 双人真机）。

真机反例：玩家说「我不去那条鬼船，往有灯光的岸划」，回答是「那是幻影，海流
已经卷过去了，再划也出不去」——**没有检定、没有代价**，玩家连着两拍说同一件事
而世界一动不动。

🔴 这是「一条规则写完先问它有没有反方向」的第三次（前两次是"失败也给"与
"治疗回血"）：规则 4e 只写了「玩家去了剧本里没有的地方 ⇒ 建即兴地点」这个
正方向，反方向**一个字都没有** ⇒ 模型当时不是不遵守，是无规则可依。

**这一层只能是概率性改进**（`exec/20 §1.29`）：代码判不了"这一拍到底给没给
代价"。所以这里守的不是模型会照做，而是**那段文本确实在、而且写全了**——
尤其三条走得通的出路，因为「纯否定的收窄会压死整片能力」。
"""

from __future__ import annotations

from app.core.keeper.capabilities import prompt_blocks
from app.core.keeper.capabilities.closure.remaining import STALL_PUSH_THRESHOLD, _stalled_line


def _rules_text() -> str:
    return "\n".join(b.text for b in prompt_blocks("rules"))


def test_the_rule_is_actually_assembled_into_the_decision_prompt() -> None:
    """🔴 **变异检验**：把 `_RULE_REFUSED_PLACE` 从 `PROMPT_BLOCKS` 里摘掉，
    这条当场红——「写了个 PromptBlock 但没注册」是本仓库的常见形态。"""
    assert "4e-2" in _rules_text()


def test_all_three_ways_out_are_spelled_out() -> None:
    """🔴 **纯否定的收窄会压死整片能力**（`format_attempts` / SAN 两处先例）：
    只说"不许宣告他去不了"而不说该怎么办，模型只会换一种方式僵住。

    🔴 **断言的子串必须选得连反向文本都装不下**：第一版写的是 `"让他去" in text`，
    而把 ① 改成「别让他去」之后**它照样通过**——反向文本把正向子串整个包住了，
    变异体大摇大摆活下来。这是「守护测试自己会瞎掉」的第五种写法。
    """
    text = _rules_text()
    assert "① **让他去**（默认）" in text  # 按 4e 建即兴地点，让他扑个空
    assert "② **拦在骰子上**" in text  # 真要拦就发检定，失败按 1d 给代价
    assert "③ **让阻力有形状**" in text  # 阻力要是能被应对的东西


def test_it_says_a_railroaded_module_is_not_an_excuse() -> None:
    """真机那次模型的理由正是「剧本只有这一条路」——不点破这一点，
    它下次照旧拿剧本当挡箭牌。"""
    assert "剧本是单行道不构成理由" in _rules_text()


def test_it_still_leaves_room_to_refuse_the_impossible() -> None:
    """反过来也不能矫枉过正：「我飞过去」必须还能拒绝，否则这条规则会逼着
    模型什么都答应。出路指向已有的规则 2，不新造一套。"""
    text = _rules_text()
    assert "物理上根本做不到" in text
    assert "按规则 2 办" in text


def test_the_stall_push_no_longer_licenses_cancelling_a_declared_direction() -> None:
    """🔴 **加了门要回头改被绕过的那句话**（2026-08-18 三条 prompt 判据之一）。

    停滞硬要求里的「① 让路到头」读起来正好是 4e-2 禁止的那件事的许可证。
    它本身没错——那是"没人指定方向"时的走法——但必须把作用域写出来，
    否则模型会拿它当豁免。

    **变异检验**：删掉 `_stalled_line` 里那两行限定，这条当场红。
    """
    pushed = _stalled_line(STALL_PUSH_THRESHOLD)
    assert "让路到头" in pushed, "前提：这条选项还在"
    assert "4e-2" in pushed
    assert "没人指定方向" in pushed
