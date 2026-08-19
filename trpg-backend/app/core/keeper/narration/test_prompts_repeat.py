"""叙事职责 2b：同一拍的第二段不许把第一段重演一遍（2026-08-18 双人真机）。

真机形态：第一段里 NPC 说「等船身贴过来再抛，别钩在那些黑洞边上」，第二段又写
「别硬顶，等船身贴过来再抛钩」——同一句话、同一拍、隔了三行。

🔴 **动手前先证伪了一个更省事的假设**：历史重放会把旧叙事截断到
`HISTORY_NARRATION_CLIP`（160 字），我本来以为那句台词被截掉了、模型压根没
看见。查了真机那条 `narration.push`——**全长 153 字，一个字没截，台词在第 119
字**；而且第二段自己用了「绳子放在膝上」这个只有第一段才有的细节。⇒ 它看见了，
照样重复 ⇒ 这是规则缺口不是结构缺陷，`exec/20 §1.30`。

这一层只能是概率性改进，所以这里守的是**那段文本在、而且写全了**。
"""

from __future__ import annotations

from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.memory.history import HISTORY_NARRATION_CLIP
from app.core.keeper.narration.prompts import build_narrator_instructions

_MODULE = ScenarioModule.model_validate(
    {
        "meta": {"id": "repeat-fixture", "title": "试验模组"},
        "kp_truth": {"summary": "无。"},
        "player_intro": "你在船上。",
        "nodes": [{"id": "deck", "title": "甲板", "kp_text": "空的。"}],
    }
)


def _instructions() -> str:
    return build_narrator_instructions(_MODULE)


def test_the_rule_is_in_the_narrator_prompt() -> None:
    """🔴 **变异检验**：删掉 2b 那一段，这条当场红。"""
    assert "2b." in _instructions()


def test_it_names_the_two_call_shape_not_just_generic_repetition() -> None:
    """「别重复」太泛，模型会理解成"别用同一个词"。要点破的是**同一拍被调用两次**
    这个具体结构——第二段的起点是第一段停住的地方。"""
    text = _instructions()
    assert "同一拍里你可能被调用两次" in text
    assert "第一段停住的地方" in text


def test_it_says_what_to_write_instead() -> None:
    """🔴 **纯否定的收窄会压死整片能力**：只说"别重演"，模型会退化成写一段
    什么都没有的景。必须给出"结算之后确实没变，就把没变写出来"这条出路。"""
    text = _instructions()
    assert "那该写什么" in text
    assert "僵持住了" in text  # 「没变」也是内容，不是留白


def test_the_existing_opening_rule_is_untouched() -> None:
    """规则 2 管的是「别重述**开场**」，2b 管的是「别重演**上一段**」——
    两条方向不同、不许合并。合并过的下场见判据全集「一份数据扮演两个角色」。
    """
    text = _instructions()
    assert "重述开场已交代过的街景" in text


def test_the_clip_is_long_enough_that_the_model_really_did_see_it() -> None:
    """🔴 钉住上面 docstring 里那个证伪：真机那段 153 字、台词在第 119 字。

    截断长度一旦被调到 119 以下，"模型看得见"这个前提就不再成立，那时 2b 就是
    在要求它避开一件它看不到的事——**这条会先红**，提醒去重新量一遍。
    """
    assert HISTORY_NARRATION_CLIP > 119
