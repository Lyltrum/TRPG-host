"""角色卡摘要进守秘人上下文（exec/23 #55）。

真人实测：玩家问「我是谁」，守秘人现编了一段个人史（警察、私酒、某人作证）
当成既成事实。根因不是模型爱编——是**它的上下文里关于玩家只有"名字 + 职业"
两项**，属性、技能、背景一个字都没有。

这份用例钉住"卡确实进了 prompt"，以及"卡上没有的东西不会被悄悄补齐"。
"""

from app.core.coc7_content import build_coc7_ruleset
from app.core.keeper.sheet_digest import format_sheet
from app.models.room import Character

RULESET = build_coc7_ruleset()


def _character(**overrides) -> Character:  # noqa: ANN003
    defaults = {
        "name": "张家豪",
        "occupation": "罪犯-独行罪犯",
        "age": 32,
        "attributes": {"STR": 60, "DEX": 70, "EDU": 50},
        "derived_stats": {"HP": 14, "SAN": 50, "MP": 10},
        "skills": {"stealth": 65, "locksmith": 55, "spot-hidden": 40},
        "background": "",
        "background_detail": None,
    }
    defaults.update(overrides)
    return Character(**defaults)


def test_sheet_carries_occupation_vitals_and_top_skills() -> None:
    text = format_sheet("张家豪", _character(), RULESET)
    assert "罪犯-独行罪犯" in text
    assert "32岁" in text
    assert "HP 14" in text and "SAN 50" in text
    # 技能按值排序，且渲染成**中文名**而不是 id——prompt 里出现 `spot-hidden`
    # 对叙事模型没有意义
    assert "潜行 65" in text
    assert "stealth" not in text


def test_empty_background_is_stated_not_omitted() -> None:
    """🔴 空背景要**说出来**。省略这一行等于让模型自己填空，而它填出来的
    是一段以既成事实口吻讲述的、谁都没同意过的个人史（真人实测原文）。"""
    text = format_sheet("张家豪", _character(), RULESET)
    assert "背景：未填写" in text


def test_background_is_rendered_when_the_player_wrote_one() -> None:
    text = format_sheet(
        "张家豪",
        _character(
            background="码头长大，欠过高利贷。",
            background_detail={"ideology": "不信任警察", "significantPeople": "妹妹"},
        ),
        RULESET,
    )
    assert "码头长大" in text
    assert "信念：不信任警察" in text
    assert "重要之人：妹妹" in text


def test_unknown_background_keys_are_kept_verbatim() -> None:
    """加字段时不静默丢内容——标签表查不到就用原键名。"""
    text = format_sheet("张家豪", _character(background_detail={"newField": "某段设定"}), RULESET)
    assert "newField：某段设定" in text


def test_player_without_a_character_is_marked() -> None:
    assert format_sheet("张家豪", None, RULESET) == "张家豪（未建卡）"
    assert format_sheet("张家豪", _character(name=""), RULESET) == "张家豪（未建卡）"


def test_unknown_skill_id_falls_back_to_the_id() -> None:
    """技能表里查不到的 id 原样显示。宁可露一个丑 id，也不要静默丢掉一项能力
    ——静默丢掉的话，裁决器会以为他不会这件事。"""
    text = format_sheet("张家豪", _character(skills={"no-such-skill": 80}), RULESET)
    assert "no-such-skill 80" in text


# ── 它确实进了局面块（不然上面几条只是在测一个没人用的函数）──


def test_sheet_reaches_the_turn_input_block() -> None:
    """裁决与叙事共用 `format_turn_input` 的名单块，卡摘要必须出现在里面。"""
    from app.core.keeper.prompts import format_turn_input

    sheet = format_sheet("张家豪", _character(), RULESET)
    block = format_turn_input(
        keeper_state=None,
        history_lines=[],
        roster=[sheet],
        player_nickname="张家豪",
        utterance="我是谁",
    )
    assert "罪犯-独行罪犯" in block
    assert "潜行 65" in block
    assert "背景：未填写" in block
