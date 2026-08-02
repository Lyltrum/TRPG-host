"""裁决器 system prompt 的技能/属性权威名称表——真人实测反复复现过裁决器
把"侦察"写成"侦查"、"闪避"写成"闪躲"，根因排查后发现最大的一处根本不是
模型自由发挥：prompt 自己的规则说明/输出示例里就写死了错误的技能名
（"搜索房间→侦查"），模型只是忠实执行了我们自己教它的错误——这类回归
最容易被忽视，专门守一条。
"""

from pathlib import Path

from app.core.coc7.content import build_coc7_ruleset
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.narration.prompts import build_adjudicator_instructions, render_skill_reference
from app.core.keeper.primitives.skills import resolve_skill_id, skill_id_catalog

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")


def test_render_skill_reference_lists_all_skills_and_attributes() -> None:
    """id 表：每一项都要给出 `id=中文名` 两侧（exec/17）。

    id 是裁决器唯一被允许填进 `skill_id` 的取值；中文名仍要给，因为
    narration_guidance 那些给人看的文字里用的是名字。
    """
    ruleset = build_coc7_ruleset()
    text = render_skill_reference(ruleset)
    for skill in ruleset.skills:
        assert f"{skill.id}={skill.name}" in text
    for attr in ruleset.attributes:
        assert f"{attr.key}={attr.label}" in text


def test_adjudicator_instructions_use_canonical_skill_names_not_known_wrong_synonyms() -> None:
    """守住此前真实发生过的三处硬编码错误名（"侦查"应为"侦察"）——这三处
    以前直接把错误名字写进了规则说明/输出示例/结尾提示,不是模型自由发挥。
    只断言这次改过的模板原文,不断言整份 prompt 不含"侦查"——`render_full`
    拼进来的剧本内容是任意的,不该被这条测试连带约束（剧本原文措辞不归
    这次改动管）。"""
    ruleset = build_coc7_ruleset()
    module = load_module(_FIXTURE_MODULE)
    text = build_adjudicator_instructions(module, ruleset)

    assert "搜索房间→侦察" in text
    assert "直接裁定侦察" in text
    assert '"skill_id": "spot-hidden"' in text
    assert "`skill_id` 必须原样取自上面权威 id 表的等号左边" in text


def test_adjudicator_instructions_contain_skill_reference_table() -> None:
    ruleset = build_coc7_ruleset()
    module = load_module(_FIXTURE_MODULE)
    text = build_adjudicator_instructions(module, ruleset)

    assert "技能/属性权威 id 表" in text
    for skill in ruleset.skills:
        assert f"{skill.id}={skill.name}" in text


def test_skill_id_catalog_is_a_closed_whitelist() -> None:
    """白名单 = 92 个技能 id + 9 个属性 key（exec/17）。

    这是裁决器唯一被允许写进 `checks[].skill_id` 的取值集合。它随规则版本
    变、不随模组变——这正是它能当标识符而中文名不能的原因。
    """
    ruleset = build_coc7_ruleset()
    catalog = skill_id_catalog(ruleset)
    assert len(catalog) == len(ruleset.skills) + len(ruleset.attributes)
    assert catalog["spot-hidden"] == "侦察"
    assert catalog["CON"] == "体质"


def test_resolve_skill_id_refuses_chinese_names() -> None:
    """🔴 中文名不是 id——解析器不能替模型把"侦察"猜回 spot-hidden。

    猜回去就等于又养了一张同义词表，而那正是 exec/17 要拆掉的东西。
    调用方拿到 None 会打 `keeper_skill_id_fallback`，走显式回退路径。
    """
    ruleset = build_coc7_ruleset()
    assert resolve_skill_id(ruleset, "侦察") is None
    assert resolve_skill_id(ruleset, "侦查") is None
    assert resolve_skill_id(ruleset, "") is None
    # id 本身大小写不敏感（模型偶尔写 Spot-Hidden）
    assert resolve_skill_id(ruleset, "Spot-Hidden") == "侦察"
