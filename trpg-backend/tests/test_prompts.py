"""裁决器 system prompt 的技能/属性权威名称表——真人实测反复复现过裁决器
把"侦察"写成"侦查"、"闪避"写成"闪躲"，根因排查后发现最大的一处根本不是
模型自由发挥：prompt 自己的规则说明/输出示例里就写死了错误的技能名
（"搜索房间→侦查"），模型只是忠实执行了我们自己教它的错误——这类回归
最容易被忽视，专门守一条。
"""

from pathlib import Path

from app.core.coc7_content import build_coc7_ruleset
from app.core.keeper.module_loader import load_module
from app.core.keeper.prompts import build_adjudicator_instructions, render_skill_reference

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")


def test_render_skill_reference_lists_all_skills_and_attributes() -> None:
    ruleset = build_coc7_ruleset()
    text = render_skill_reference(ruleset)
    for skill in ruleset.skills:
        assert skill.name in text
    for attr in ruleset.attributes:
        assert attr.label in text


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
    assert '"skill": "侦察"' in text
    assert "技能/属性名必须原样取自上面的权威名称表" in text


def test_adjudicator_instructions_contain_skill_reference_table() -> None:
    ruleset = build_coc7_ruleset()
    module = load_module(_FIXTURE_MODULE)
    text = build_adjudicator_instructions(module, ruleset)

    assert "技能/属性权威名称表" in text
    for skill in ruleset.skills:
        assert skill.name in text
