"""装备申辩：玩家给一句来路，主持人再判一次（2026-08-19）。

## 🔴 为什么加这一步

真机量出来的：1925 年的图书管理员带把 `.32 左轮`被**稳定拦下 3/3**，会计的
`.38` 同样（而私家侦探、农夫、医生、教授的枪全部放行）。模型的标准是连贯的
——它在问"这个职业有没有说得通的持枪来路"——但那跟 prompt 自己写的尺度矛盾：
「**一件东西只要有一个说得通的来路，就算合理**」。

真人桌上「图书管理员哪来的枪」从来不是主持人单方面判定，而是玩家给个理由、
主持人点头。**缺的不是更松的尺度，是那句理由。**

这里只守**代码这一半**（说明进得了 prompt、`field` 定位得到是哪件）；
"模型认不认这条理由"是语义，归 `exec/20` 的概率性改进，靠真机探针验
（已验：给了成立的理由放行 3/3，理由本身不成立的仍拒 3/3）。
"""

from __future__ import annotations

from app.core.equipment_check import RejectedItem, build_prompt, rejection_message


def _prompt(equipment: list[str], notes: dict[str, str] | None = None) -> str:
    """真机探针里那个稳定被拦的人：1925 年阿卡姆的图书管理员，信用 30。"""
    return build_prompt(
        equipment=equipment,
        occupation="图书管理员",
        age=35,
        residence="美国马萨诸塞州阿卡姆",
        birthplace="美国马萨诸塞州阿卡姆",
        credit_rating=30,
        era="1925年，美国马萨诸塞州阿卡姆",
        notes=notes,
    )


def test_the_players_explanation_reaches_the_prompt() -> None:
    """🔴 **变异检验**：把 `build_prompt` 里拼 note 那一支去掉，这条当场红。

    这是「加了字段没有消费方」最容易发生的地方——DTO 有、端点收得到、service
    传下来了，只有最后拼进那段文本的地方漏了，而两头都不会变红。
    """
    prompt = _prompt([".32 左轮手枪"], {".32 左轮手枪": "我父亲留下的，他是一战老兵"})
    assert "我父亲留下的，他是一战老兵" in prompt
    assert "玩家说明" in prompt


def test_items_without_an_explanation_look_exactly_as_before() -> None:
    """没给说明的那些**一个字都不该变**——否则第一次提交的判断就被这次改动动了。"""
    plain = _prompt(["手电筒", "笔记本"])
    with_empty = _prompt(["手电筒", "笔记本"], {"手电筒": "   "})
    assert plain == with_empty
    assert "玩家说明" not in plain


def test_an_explanation_for_something_not_on_the_list_is_ignored() -> None:
    """说明的键对不上清单里的物品时不该凭空冒出来（改了装备名又没改说明）。"""
    prompt = _prompt(["手电筒"], {"左轮手枪": "父亲留下的"})
    assert "父亲留下的" not in prompt


def test_the_prompt_tells_the_model_a_bad_explanation_still_fails() -> None:
    """🔴 **纯放宽会压死整片能力**：只说"给了理由就放行"，1925 年的
    「公司发的手机」也会过——那等于把校验拆了。

    **变异检验**：删掉 prompt 里"只有说明本身也不成立时才继续拒"那一段，
    这条当场红。（真机侧已验：那两条反例仍被拒 3/3。）
    """
    from app.core.equipment_check import _INSTRUCTIONS

    assert "玩家的说明" in _INSTRUCTIONS
    # 断言选得连反例都装不下：光有"放行"两个字，反向文本也包含它
    assert "只有说明**本身**在这个时代、这个地方也不成立时才继续拒" in _INSTRUCTIONS


def test_the_issue_says_which_item_it_was() -> None:
    """🔴 前端要就地给**这件**东西一个输入框，只有一句拼好的话定位不到。

    **变异检验**：把 `field` 改回 `"equipment"`，这条当场红。
    """
    import inspect

    from app.service.character import _equipment_issues

    source = inspect.getsource(_equipment_issues)
    assert 'field=f"equipment.{rejected.item}"' in source


def test_the_message_still_reads_like_a_sentence() -> None:
    """退化保证：给玩家看的那句话没被这次改动弄坏。"""
    msg = rejection_message(
        RejectedItem(item="手机", reason="1925 年还没有手机", alternatives=["怀表", "电报"])
    )
    assert "「手机」" in msg
    assert "1925 年还没有手机" in msg
    assert "怀表、电报" in msg
