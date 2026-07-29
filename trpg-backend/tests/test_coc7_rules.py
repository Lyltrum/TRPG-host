"""COC7 建卡计算/校验模块（issue #84 S2；issue #112 改为参数注入）的单元测试：
公式求值 + 一张合法卡返回空校验报告 + 六类非法各一条能被独立拦下。

用「会计师」职业（id=1，`skill_points_formula="EDU*4"`，信用评级 [30,70]）当
固定夹具，8 项属性全部取 50 让预算数字好算：职业技能点预算 = EDU*4 = 200，
兴趣技能点预算 = INT*2 = 100。

⚠️ issue #114 之后会计师的职业技能是 **6 项固定**（accounting/law/library-use/
listen/persuade/spot-hidden）**+ 一个「任意其他两项」自选槽**，跟规则书一致。
此前是 8 项固定（多出 psychology/science-mathematics），那是移植时被规整出来的
——所以凡是依赖「哪些技能算职业技能」的用例，数字都跟改动前不同，不是算错了。

issue #112：这个模块的公开入口现在都要求调用方传入 `RulesetRead`，多数用例
借用内置 COC7 的完整规则数据（`RULESET`）当夹具——这只是「借用一份趁手的规则
数据」，不代表 `coc7_rules` 本身认识或依赖 COC7；下方
`test_compute_preview_works_with_a_minimal_non_coc7_ruleset` 额外用一份跟
COC7 毫无关系的最小 ruleset 证明这一点。
"""

from app.core.coc7_content import build_coc7_ruleset
from app.core.coc7_rules import (
    GENERATION_ROLL_POOL,
    SkillPointsBudget,
    compute_derived_stats,
    compute_preview,
    evaluate_skill_base,
    evaluate_skill_points_formula,
    validate_character,
)
from app.dto.game import (
    AttributeSpec,
    OccupationSpec,
    RulesetRead,
    SkillChoiceSlot,
    SkillSpec,
)

RULESET = build_coc7_ruleset()

ATTRS = {
    "STR": 50,
    "CON": 50,
    "POW": 50,
    "DEX": 50,
    "APP": 50,
    "SIZ": 50,
    "INT": 50,
    "EDU": 50,
    "LUCK": 50,
}
ACCOUNTANT_ID = 1
ACCOUNTANT_NAME = "会计师"


def test_derived_stats_formulas() -> None:
    # DB 是字符串（"0"），Build 是整数（0）——两者由同一张表查出但值不同，
    # 旧实现曾把两者塞成同一个字符串，这里顺带钉死类型不会退化回去。
    stats = compute_derived_stats(ATTRS)
    assert stats == {"HP": 10, "MP": 10, "SAN": 50, "DB": "0", "Build": 0, "MOV": 8}


def test_derived_stats_move_small_and_large() -> None:
    # COC7 规则：STR/DEX 都小于 SIZ（体格相对弱小）MOV=7；都大于 SIZ（体格
    # 相对高大灵活）MOV=9（PR #85 review #2：之前这两个分支的返回值是反的）。
    small = compute_derived_stats({**ATTRS, "STR": 30, "DEX": 30, "SIZ": 60})
    assert small["MOV"] == 7
    large = compute_derived_stats({**ATTRS, "STR": 80, "DEX": 80, "SIZ": 40})
    assert large["MOV"] == 9


def test_damage_bonus_build_table() -> None:
    stats_low = compute_derived_stats({**ATTRS, "STR": 10, "SIZ": 10})
    assert (stats_low["DB"], stats_low["Build"]) == ("-2", -2)

    stats_mid = compute_derived_stats({**ATTRS, "STR": 90, "SIZ": 90})
    assert (stats_mid["DB"], stats_mid["Build"]) == ("+1D6", 2)


def test_damage_bonus_build_table_beyond_1d6_is_not_hardcoded_1d8() -> None:
    """🔴 回归用例：旧实现里 `total > 204` 无条件返回 "+1D8"——COC7 官方表里
    根本没有这一档，204 之后应该按 coc-char-gen `engine.js::damageBonusAndBuild`
    的完整表继续延伸（+2D6/+3D6/+4D6/……），不是卡死在一个不存在的值上。"""
    # sum = 240，落在 (204, 284] 这一档：应该是 +2D6/build 3，不是 +1D8。
    stats_2d6 = compute_derived_stats({**ATTRS, "STR": 120, "SIZ": 120})
    assert (stats_2d6["DB"], stats_2d6["Build"]) == ("+2D6", 3)

    # sum = 300，落在 (284, 364] 这一档：+3D6/build 4。
    stats_3d6 = compute_derived_stats({**ATTRS, "STR": 150, "SIZ": 150})
    assert (stats_3d6["DB"], stats_3d6["Build"]) == ("+3D6", 4)

    # sum = 500，超出表格列出的最后一档（+4D6/build 5，上限 444），按公式
    # 每 80 点再 +1D6、build+1 延伸：extra = (500-445)//80 + 1 = 1。
    stats_extended = compute_derived_stats({**ATTRS, "STR": 250, "SIZ": 250})
    assert (stats_extended["DB"], stats_extended["Build"]) == ("+5D6", 6)


def test_derived_stats_mov_age_penalty() -> None:
    """MOV 要扣年龄惩罚（coc-char-gen `engine.js::movementRate`）：不传 age
    时保持旧行为（不扣），传了 age 就按年龄档扣，且不会扣到 0 以下。"""
    base = {**ATTRS, "STR": 80, "DEX": 80, "SIZ": 40}  # MOV 基础值 9

    assert compute_derived_stats(base)["MOV"] == 9
    assert compute_derived_stats(base, age=25)["MOV"] == 9  # 20-39 档无 MOV 惩罚
    assert compute_derived_stats(base, age=45)["MOV"] == 8  # 40-49 档 MOV-1
    assert compute_derived_stats(base, age=85)["MOV"] == 4  # 80-89 档 MOV-5


def test_evaluate_skill_base_handles_fixed_formula_and_divisor() -> None:
    assert evaluate_skill_base(25, ATTRS) == 25
    assert evaluate_skill_base("EDU", ATTRS) == 50
    assert evaluate_skill_base("DEX/2", ATTRS) == 25


def test_evaluate_skill_points_formula_single_and_multi_term() -> None:
    assert evaluate_skill_points_formula("EDU*4", ATTRS) == 200
    assert evaluate_skill_points_formula("EDU*2+DEX*2", ATTRS) == 200


def test_evaluate_skill_points_formula_max_term_takes_higher_attribute() -> None:
    attrs = {**ATTRS, "STR": 40, "DEX": 60, "EDU": 50}
    # EDU*2 + max(STR,DEX)*2 = 50*2 + 60*2 = 100 + 120 = 220（验证取较高的 DEX）
    assert evaluate_skill_points_formula("EDU*2+MAX(STR,DEX)*2", attrs) == 220


def test_evaluate_skill_points_formula_max_term_with_three_attributes() -> None:
    attrs = {**ATTRS, "APP": 30, "DEX": 45, "STR": 70}
    # EDU*2 + max(APP,DEX,STR)*2 = 50*2 + 70*2 = 100 + 140 = 240（三选一取 STR）
    assert evaluate_skill_points_formula("EDU*2+MAX(APP,DEX,STR)*2", attrs) == 240


def test_evaluate_skill_points_formula_rejects_unparseable_string() -> None:
    import pytest

    with pytest.raises(ValueError):
        evaluate_skill_points_formula("EDU*4 或 STR*2", ATTRS)


def test_valid_card_has_empty_validation_report() -> None:
    skills = {
        "accounting": 55,
        "law": 55,
        "library-use": 70,
        "listen": 70,
        "dodge": 50,  # 非职业技能，DEX/2=25 基础值，分配 25 点
        "occult": 30,  # 非职业技能，分配 25 点
        # 会计师信用区间 [30,70]：下限 30 点算职业点负担，超出下限的 20 点
        # （50-30）算兴趣点负担（COC7 官方裁定，见下方专项测试）。
        "credit-rating": 50,
    }
    result = compute_preview(RULESET, ATTRS, ACCOUNTANT_ID, skills)

    assert result.validation == []
    # 职业技能原始需求 250（accounting/law/library-use/listen 各分配 50，
    # dodge/occult 各 25——issue #114：会计师按规则书带「任意其他两项」自选槽，
    # 这两项非固定技能占住了槽，改吃职业点）+ 信用下限 30 = 280，超出职业
    # 预算 200 达 80 点。issue #22/wizard-bugfix-round5：瀑布式记账下这 80
    # 点溢出转记进兴趣桶，职业桶被封顶在预算 200（不再像旧实现那样直接显示
    # 280/-80 这种"超支"的记账）。
    assert result.occupation_skill_points == SkillPointsBudget(budget=200, spent=200, remaining=0)
    # 兴趣桶 = 信用超出下限的 20 + 职业桶溢出的 80 = 100，同样打满预算。
    assert result.interest_skill_points == SkillPointsBudget(budget=100, spent=100, remaining=0)
    # 76 前端原有 +3 悬空引用补齐 +1 信用评级 −1 重复的导航
    # +8 目录缺口 +3 学识族 +2 链锯/热气球（issue #114）
    assert len(result.skill_view) == 76 + 3 + 1 - 1 + 8 + 3 + 2

    # complete_character 用的是按名字查职业的版本，结果应该一致
    assert validate_character(RULESET, ATTRS, ACCOUNTANT_NAME, skills) == []


def test_skill_points_exceeded_alone() -> None:
    # issue #22/wizard-bugfix-round5：瀑布式记账下，职业技能超出职业预算的
    # 部分会先转记进兴趣桶——当总花费本身也超出总预算时，这部分溢出会把
    # 兴趣桶也一并推过预算，所以这里现在会同时触发 INTEREST_POINTS_EXCEEDED
    # 和 SKILL_POINTS_EXCEEDED（两个数学上是等价条件，见 coc7_rules.py 里
    # 瀑布分配那段注释的推导：total_spent > total_budget 时两条同时成立）。
    # 这不改变这张卡"不合法"的最终结论，只是多暴露了一条同样成立的校验。
    skills = {
        "accounting": 99,
        "law": 99,
        "library-use": 99,
        "persuade": 99,
        "credit-rating": 50,  # 避免信用未填触发 CREDIT_OUT_OF_RANGE 掩盖了本测试要验的错误
    }
    issues = validate_character(RULESET, ATTRS, ACCOUNTANT_NAME, skills)
    codes = [issue.code for issue in issues]
    assert codes == ["INTEREST_POINTS_EXCEEDED", "SKILL_POINTS_EXCEEDED"]


def test_occupation_skills_may_overflow_into_interest_points() -> None:
    """🔴 职业技能上的点数超过职业预算是**合法**的，超出部分由兴趣点承担。

    COC7 里兴趣点可以花在任何技能上（包括职业技能），所以「职业点单独超了」
    不是拒绝理由——闸门是总预算。这条和上一条互为对照：上一条超的是总预算
    必须拒，这一条只超职业池、总预算没超，必须放行。

    issue #22/wizard-bugfix-round5 之前，这种情况下 `occupation_spent` 会
    直接超过 `occupation_budget`（记账"超支"）；瀑布式记账落地后，职业桶
    恒被封顶在预算内，超出部分改为转记进兴趣桶——用例改成直接验证这个
    "恰好高出溢出量"的瀑布行为，而不是允许职业桶本身超支。
    """
    # 会计师：职业点 EDU*4=200，兴趣点 INT*2=100。accounting/law 都是固定
    # 职业技能（不涉及自选槽，避免槽分配影响这里要验的纯瀑布记账）。
    baseline_skills = {
        "accounting": 90,  # base 5  → allocated 85
        "law": 60,  # base 5  → allocated 55
        "credit-rating": 30,  # 下限，全额记职业点，不产生兴趣负担
    }
    overflow_skills = {
        "accounting": 99,  # base 5  → allocated 94
        "law": 99,  # base 5  → allocated 94
        "credit-rating": 30,
    }

    baseline = compute_preview(
        RULESET, attributes=ATTRS, occupation_id=ACCOUNTANT_ID, skills=baseline_skills
    )
    overflow = compute_preview(
        RULESET, attributes=ATTRS, occupation_id=ACCOUNTANT_ID, skills=overflow_skills
    )

    # 基线：职业技能原始需求 85+55=140，信用下限 30，合计 170，没有超过职业
    # 预算 200——不触发瀑布，职业桶=170，兴趣桶=0，跟瀑布式改动前的行为
    # 完全一致（没有溢出就没有行为变化）。
    assert baseline.occupation_skill_points == SkillPointsBudget(
        budget=200, spent=170, remaining=30
    )
    assert baseline.interest_skill_points == SkillPointsBudget(budget=100, spent=0, remaining=100)
    assert baseline.validation == []

    # 溢出：职业技能原始需求 94+94=188，信用下限 30，合计 218；职业预算给
    # 信用留出 30 后只剩 170 可用，188 用不完，溢出 18 点。职业桶被瀑布封顶
    # 在预算 200（不再像旧实现那样显示"超支"），溢出的 18 点转记进兴趣桶。
    assert overflow.occupation_skill_points == SkillPointsBudget(budget=200, spent=200, remaining=0)
    assert overflow.interest_skill_points == SkillPointsBudget(budget=100, spent=18, remaining=82)
    assert overflow.validation == []

    # 核心断言（对应 wizard-bugfix-round5 的要求）：溢出场景的兴趣桶，恰好
    # 比"不溢出"的基线场景高出 18 点——一分不多一分不少，正是从职业桶瀑布
    # 转移过来的那部分，而不是凭空多算出来的。
    assert overflow.interest_skill_points.spent - baseline.interest_skill_points.spent == 18
    # 职业桶恒被瀑布分配封顶在预算内，不会再出现"spent > budget"这种旧行为。
    assert overflow.occupation_skill_points.spent <= overflow.occupation_skill_points.budget
    # 总花费不变的不变量：两个场景各自的两桶之和，跟"总预算够不够"这条闸门
    # 依然一致（这条闸门本身不受瀑布式记账影响，见 test_skill_points_exceeded_alone）。
    assert (
        overflow.occupation_skill_points.spent + overflow.interest_skill_points.spent
        <= overflow.occupation_skill_points.budget + overflow.interest_skill_points.budget
    )


def test_waterfall_conserves_total_skill_points_regardless_of_bucket() -> None:
    """瀑布式记账（issue #22/wizard-bugfix-round5）只是把同一笔总花费在
    职业桶/兴趣桶之间重新分配，不应该改变"总共花了多少技能点"这个数——这
    个数恒等于「每一项技能实际分配了多少点」的直接算术和，跟最终落进哪个
    桶无关（哪个桶只是记账口径，不改变玩家实际花掉的点数）。

    这里刻意**不**通过对比"旧实现"来证明（旧的按技能身份二选一记账已经被
    这次改动整个替换掉，仓库里已经没有它可以拿来跑），而是直接从 `skills`
    输入用 `evaluate_skill_base` 独立算出预期总和——这笔总和的计算完全不
    依赖 `_compute` 内部怎么分桶（无论技能占不占自选槽、算不算职业技能，
    每一分点数都必然被计入 occupation_spent 或 interest_spent 二者之一，
    绝不会漏计或重复计），所以拿它跟 `_compute` 实际算出的两桶之和比较，
    就是对"瀑布式记账不改变总和"这条不变量最直接的证明。
    """
    skills = {
        "accounting": 99,  # 固定职业技能，故意分配到顶，制造溢出
        "law": 99,  # 同上
        "dodge": 70,  # 非职业技能候选（可能被自选槽吸收，但不影响总和）
        "credit-rating": 50,  # 会计师下限 30，超出的 20 点算兴趣，但仍计入总和
    }

    def _skill_base(skill_id: str) -> int:
        spec = next(s for s in RULESET.skills if s.id == skill_id)
        return evaluate_skill_base(spec.base, ATTRS)

    expected_total = (
        max(0, 99 - _skill_base("accounting"))
        + max(0, 99 - _skill_base("law"))
        + max(0, 70 - _skill_base("dodge"))
        + 50  # credit-rating=50 >= credit_min(30)，全额（不只是超出部分）计入总花费
    )

    result = compute_preview(RULESET, ATTRS, ACCOUNTANT_ID, skills)
    actual_total = result.occupation_skill_points.spent + result.interest_skill_points.spent

    assert actual_total == expected_total
    # 职业桶恒被瀑布分配封顶在预算内——这是瀑布式记账的直接结果，不再出现
    # 旧实现里"spent > budget"那种记账口径上的"超支"。
    assert result.occupation_skill_points.spent <= result.occupation_skill_points.budget


def test_compute_preview_respects_roll_pool_generation_method() -> None:
    """wizard-bugfix-round1 核心发现：`compute_preview()` 此前完全不接
    `generation_method`/`attribute_pool_total`，建卡向导的实时预览请求永远
    按点数购买法（预算 480）校验属性总和——掷点池玩家的池子总值几乎从不是
    480，几乎必然被误判为 `ATTRIBUTE_POINTS_EXCEEDED`，进而让
    `derived_stats`/两个技能点预算/`skill_view` 全部退化成空（`_compute` 属性
    校验失败时整体短路，见 `_compute` 里 `if attribute_issues: return ...`）。

    这里复用 `test_valid_card_has_empty_validation_report` 那组 ATTRS/skills
    （8 项可购买属性总和 400，≠480），只是把生成方法换成 `roll_pool` 并带上
    权威池子总值 400——回归此前完全没有测试覆盖这条路径的空白。
    """
    skills = {
        "accounting": 55,
        "law": 55,
        "library-use": 70,
        "listen": 70,
        "dodge": 50,
        "occult": 30,
        "credit-rating": 50,
    }
    result = compute_preview(
        RULESET,
        ATTRS,
        ACCOUNTANT_ID,
        skills,
        generation_method=GENERATION_ROLL_POOL,
        attribute_pool_total=400,
    )

    codes = [issue.code for issue in result.validation]
    assert "ATTRIBUTE_POINTS_EXCEEDED" not in codes
    assert "ATTRIBUTE_POOL_MISMATCH" not in codes
    assert result.validation == []
    # 属性校验没有短路：衍生值/两个技能点预算/技能列表都是正常非退化的值，
    # 跟点数购买法算出来的一样（同一组属性/技能，只是生成方法不同，规则
    # 计算本身不受影响）。
    assert result.derived_stats != {}
    # 数值口径见 test_valid_card_has_empty_validation_report（同一组 skills，
    # 瀑布式记账下职业/兴趣两桶都被打满在各自预算上）——这里只是确认换了
    # 生成方法之后规则计算本身不受影响，不重复推导。
    assert result.occupation_skill_points == SkillPointsBudget(budget=200, spent=200, remaining=0)
    assert result.interest_skill_points == SkillPointsBudget(budget=100, spent=100, remaining=0)
    assert len(result.skill_view) > 0


def test_compute_preview_respects_roll_generation_method() -> None:
    """同一个 bug 的另一条分支：服务端掷骰法（`generation_method="roll"`）
    8 项属性总和常年超过点数购买法的预算 480（均值约 457、范围 195–720），
    此前预览同样会被误判成 `ATTRIBUTE_POINTS_EXCEEDED` 而整体退化——此前也
    完全没有测试覆盖这条路径。"""
    attrs = {**ATTRS, "STR": 90, "CON": 90, "POW": 90, "DEX": 90, "APP": 90}
    skills = {"accounting": 55, "credit-rating": 30}
    result = compute_preview(RULESET, attrs, ACCOUNTANT_ID, skills, generation_method="roll")

    codes = [issue.code for issue in result.validation]
    assert "ATTRIBUTE_POINTS_EXCEEDED" not in codes
    assert result.derived_stats != {}
    assert len(result.skill_view) > 0


def test_interest_points_exceeded_alone() -> None:
    """非职业技能花费超过兴趣预算。

    issue #114 后要 5 项非职业技能才测得出来：会计师按规则书带「任意其他两项」
    自选槽，最大的两项会被槽吸收、改吃职业点，只有剩下的才算兴趣点。此前用
    2 项就能触发，是因为那时职业技能列表是编的、没有槽。
    5 项各分配 54 点 → 吸收 108、剩 162 > 兴趣预算 100；总花费 300 = 总预算，
    刚好不触发 SKILL_POINTS_EXCEEDED，保证只测出这一条。
    """
    skills = {
        "dodge": 79,
        "occult": 59,
        "climb": 74,
        "swim": 74,
        "jump": 74,
        "credit-rating": 30,
    }
    issues = validate_character(RULESET, ATTRS, ACCOUNTANT_NAME, skills)
    codes = [issue.code for issue in issues]
    assert codes == ["INTEREST_POINTS_EXCEEDED"]


def test_skill_above_cap_alone() -> None:
    skills = {"spot-hidden": 105, "credit-rating": 50}
    issues = validate_character(RULESET, ATTRS, ACCOUNTANT_NAME, skills)
    codes = [issue.code for issue in issues]
    assert codes == ["SKILL_ABOVE_CAP"]


def test_skill_below_base_alone() -> None:
    skills = {"accounting": 0, "credit-rating": 50}
    issues = validate_character(RULESET, ATTRS, ACCOUNTANT_NAME, skills)
    codes = [issue.code for issue in issues]
    assert codes == ["SKILL_BELOW_BASE"]


def test_credit_in_range_passes() -> None:
    # 会计师信用区间 [30,70]，50 在区间内，单独看不应该产出任何校验项。
    issues = validate_character(RULESET, ATTRS, ACCOUNTANT_NAME, {"credit-rating": 50})
    assert issues == []


def test_credit_missing_defaults_to_zero_and_is_rejected() -> None:
    # 不传信用评级时 current = base(0)，等价于交了 0 分，落在会计师区间
    # [30,70] 之外——这就是"必填"的实现方式。
    issues = validate_character(RULESET, ATTRS, ACCOUNTANT_NAME, {})
    codes = [issue.code for issue in issues]
    assert codes == ["CREDIT_OUT_OF_RANGE"]


def test_credit_out_of_range_alone() -> None:
    issues = validate_character(RULESET, ATTRS, ACCOUNTANT_NAME, {"credit-rating": 99})
    codes = [issue.code for issue in issues]
    assert codes == ["CREDIT_OUT_OF_RANGE"]


def test_credit_not_capped_at_99_and_skips_below_base_check() -> None:
    # 信用评级不走常规的「不能低于基础值/不能超过 99」检查，改用职业信用区间——
    # 这里用一个超过 99 的值验证它不会被误判成 SKILL_ABOVE_CAP。超出下限的
    # 120 点（150-30）全部算进兴趣点，超过兴趣预算 100，所以还会连带触发
    # INTEREST_POINTS_EXCEEDED；同时仍因超出会计师区间 [30,70] 被
    # CREDIT_OUT_OF_RANGE 拦下。
    issues = validate_character(RULESET, ATTRS, ACCOUNTANT_NAME, {"credit-rating": 150})
    codes = [issue.code for issue in issues]
    assert codes == ["INTEREST_POINTS_EXCEEDED", "CREDIT_OUT_OF_RANGE"]


def test_credit_at_min_counts_only_against_occupation_points() -> None:
    # COC7 官方裁定：信用评级的下限（credit_min）那部分点数由职业点负担。
    # 会计师信用下限是 30，信用刚好等于下限时，兴趣点完全不受影响。
    result = compute_preview(RULESET, ATTRS, ACCOUNTANT_ID, {"credit-rating": 30})
    assert result.occupation_skill_points.spent == 30
    assert result.interest_skill_points.spent == 0
    assert result.validation == []


def test_credit_above_min_excess_counts_against_interest_points() -> None:
    # 超出下限的部分（credit_value - credit_min）由兴趣点负担：会计师信用
    # 下限 30，信用调到 50 时，多出的 20 点应该落进 interest_spent，而不是
    # 继续算进 occupation_spent。
    result = compute_preview(RULESET, ATTRS, ACCOUNTANT_ID, {"credit-rating": 50})
    assert result.occupation_skill_points.spent == 30
    assert result.interest_skill_points.spent == 20
    assert result.validation == []


def test_credit_excess_counts_against_interest_budget() -> None:
    # 4 项非职业技能各分配 45 点，其中最大的两项被会计师的「任意其他两项」自选槽
    # 吸收（改吃职业点），剩下 90 点算兴趣点——**还没超**预算 100。再给信用评级
    # 分配 50 点，超出下限 30 的那 20 点也算兴趣点，总计 110 才超。
    #
    # 数值特意这么挑，是为了让这条用例真的在测「信用超出下限算兴趣点」：下面的
    # 对照断言证明不加那 20 点时是合法的，所以断言的失败只可能来自信用这部分。
    skills = {"dodge": 70, "occult": 50, "climb": 65, "swim": 65}
    baseline = validate_character(RULESET, ATTRS, ACCOUNTANT_NAME, {**skills, "credit-rating": 30})
    assert baseline == []

    issues = validate_character(RULESET, ATTRS, ACCOUNTANT_NAME, {**skills, "credit-rating": 50})
    codes = [issue.code for issue in issues]
    assert codes == ["INTEREST_POINTS_EXCEEDED"]


def test_unknown_skill_alone() -> None:
    issues = validate_character(
        RULESET, ATTRS, ACCOUNTANT_NAME, {"totally-fake-skill": 50, "credit-rating": 50}
    )
    codes = [issue.code for issue in issues]
    assert codes == ["UNKNOWN_SKILL"]


def test_invalid_attributes_missing_key_rejected() -> None:
    attrs = {k: v for k, v in ATTRS.items() if k != "EDU"}
    issues = validate_character(RULESET, attrs, ACCOUNTANT_NAME, {})
    codes = [issue.code for issue in issues]
    assert codes == ["INVALID_ATTRIBUTES"]


def test_invalid_attributes_missing_luck_rejected() -> None:
    """幸运是必填属性——建卡时必须掷出来，不能整项缺失。"""
    attrs = {k: v for k, v in ATTRS.items() if k != "LUCK"}
    issues = validate_character(RULESET, attrs, ACCOUNTANT_NAME, {})
    codes = [issue.code for issue in issues]
    assert codes == ["INVALID_ATTRIBUTES"]


def test_luck_does_not_affect_skill_point_budgets() -> None:
    """幸运不参与任何职业技能点/兴趣技能点公式——改幸运值，两条预算都不动。
    （COC7 里幸运是独立掷出的属性，只在游戏中被消耗，不换算成技能点。）"""
    baseline = compute_preview(RULESET, ATTRS, ACCOUNTANT_ID, {})
    lucky = compute_preview(RULESET, {**ATTRS, "LUCK": 99}, ACCOUNTANT_ID, {})

    assert lucky.occupation_skill_points.budget == baseline.occupation_skill_points.budget
    assert lucky.interest_skill_points.budget == baseline.interest_skill_points.budget


def test_invalid_attributes_extra_key_rejected() -> None:
    attrs = {**ATTRS, "LUK": 50}
    issues = validate_character(RULESET, attrs, ACCOUNTANT_NAME, {})
    codes = [issue.code for issue in issues]
    assert codes == ["INVALID_ATTRIBUTES"]


def test_invalid_attributes_out_of_range_rejected() -> None:
    attrs = {**ATTRS, "INT": 999}
    issues = validate_character(RULESET, attrs, ACCOUNTANT_NAME, {})
    codes = [issue.code for issue in issues]
    assert codes == ["INVALID_ATTRIBUTES"]


def test_invalid_attributes_short_circuits_and_skips_other_checks() -> None:
    # 属性不合法时应该直接返回，不会借着这份脏数据继续算出一堆其他校验项
    # （比如信用评级缺失本来也会报错，但不应该跟 INVALID_ATTRIBUTES 一起出现）。
    attrs = {**ATTRS, "STR": 0}
    issues = validate_character(RULESET, attrs, ACCOUNTANT_NAME, {})
    codes = [issue.code for issue in issues]
    assert codes == ["INVALID_ATTRIBUTES"]


def test_occupation_not_found_by_id_and_by_name() -> None:
    preview = compute_preview(RULESET, ATTRS, 9999, {})
    assert any(issue.code == "OCCUPATION_NOT_FOUND" for issue in preview.validation)

    issues = validate_character(RULESET, ATTRS, "不存在的职业", {})
    assert any(issue.code == "OCCUPATION_NOT_FOUND" for issue in issues)


def test_occupation_skill_points_budget_uses_max_of_str_dex() -> None:
    # 事务所侦探（id=30）公式是 EDU*2+MAX(STR,DEX)*2，STR40/DEX60 应按较高的
    # DEX 算：EDU*2 + DEX*2 = 50*2 + 60*2 = 220，而不是误用 STR 算出的 180。
    attrs = {**ATTRS, "STR": 40, "DEX": 60}
    result = compute_preview(RULESET, attrs, 30, {})
    assert result.occupation_skill_points.budget == 220


def test_no_occupation_selected_all_budget_is_interest_only() -> None:
    result = compute_preview(RULESET, ATTRS, None, {})
    assert result.occupation_skill_points == SkillPointsBudget(budget=0, spent=0, remaining=0)
    assert result.interest_skill_points == SkillPointsBudget(budget=100, spent=0, remaining=100)
    assert result.validation == []


# ── 属性点预算：必须区分生成方法（issue #96 决策 1）────────────────────


def test_point_buy_over_budget_is_rejected() -> None:
    """点数购买法：8 项可购买属性的总和超过预算就拒。"""
    attrs = {**ATTRS, "STR": 90, "CON": 90, "POW": 90, "DEX": 90, "APP": 90}
    # 90*5 + 50*3 = 600 > 480
    issues = validate_character(RULESET, attrs, ACCOUNTANT_NAME, {}, generation_method="pointbuy")
    assert "ATTRIBUTE_POINTS_EXCEEDED" in [issue.code for issue in issues]


def test_rolled_attributes_over_point_buy_budget_are_allowed() -> None:
    """🔴 掷骰法不受点数购买预算约束。

    这条和上一条是一对：掷骰法 8 项总和均值约 457、理论范围 195–720，本来就
    经常超过 480。如果不区分生成方法、无条件拿预算去卡，合法掷出来的角色卡
    会被判成非法，等于废掉 roll-attributes 端点。
    """
    attrs = {**ATTRS, "STR": 90, "CON": 90, "POW": 90, "DEX": 90, "APP": 90}
    issues = validate_character(RULESET, attrs, ACCOUNTANT_NAME, {}, generation_method="roll")
    assert "ATTRIBUTE_POINTS_EXCEEDED" not in [issue.code for issue in issues]


def test_luck_is_excluded_from_the_attribute_point_budget() -> None:
    """幸运不占属性点预算：把它拉满也不该让总预算超支。"""
    attrs = {**ATTRS, "LUCK": 99}
    issues = validate_character(RULESET, attrs, ACCOUNTANT_NAME, {}, generation_method="pointbuy")
    assert "ATTRIBUTE_POINTS_EXCEEDED" not in [issue.code for issue in issues]


def test_point_buy_attribute_below_min_is_rejected() -> None:
    """点数购买法下单项属性有 [10, 90] 区间，低于下限要拒——这个边界此前
    只有前端在管，后端放行到 1。"""
    issues = validate_character(RULESET, {**ATTRS, "STR": 5}, ACCOUNTANT_NAME, {}, "pointbuy")
    assert "INVALID_ATTRIBUTES" in [issue.code for issue in issues]


def test_rolled_attribute_below_point_buy_min_is_allowed() -> None:
    """掷骰法不套 [10, 90]：3d6*5 最低能掷出 15，但兜底区间放到 [1, 99]，
    不该拿点数购买法的下限去卡骰子结果。"""
    issues = validate_character(RULESET, {**ATTRS, "STR": 5}, ACCOUNTANT_NAME, {}, "roll")
    assert "INVALID_ATTRIBUTES" not in [issue.code for issue in issues]


def test_roll_pool_allows_a_valid_allocation() -> None:
    """掷点池法：8 项可购买属性总和正好等于池子总值，单项落在
    [ROLL_POOL_ATTRIBUTE_MIN, ROLL_POOL_ATTRIBUTE_MAX] 且是 5 的倍数——合法。

    ATTRS 里 8 项可购买属性（不含幸运）都是 50，总和 400。
    """
    issues = validate_character(
        RULESET,
        ATTRS,
        ACCOUNTANT_NAME,
        {},
        generation_method=GENERATION_ROLL_POOL,
        attribute_pool_total=400,
    )
    assert "ATTRIBUTE_POOL_MISMATCH" not in [issue.code for issue in issues]
    assert "INVALID_ATTRIBUTES" not in [issue.code for issue in issues]


def test_roll_pool_total_mismatch_is_rejected() -> None:
    """分配总和跟服务端权威记下的池子总值对不上要拒——这条校验存在的意义
    就是不能只信任客户端报的分配结果。"""
    issues = validate_character(
        RULESET,
        ATTRS,  # 8 项总和 400
        ACCOUNTANT_NAME,
        {},
        generation_method=GENERATION_ROLL_POOL,
        attribute_pool_total=450,
    )
    assert "ATTRIBUTE_POOL_MISMATCH" in [issue.code for issue in issues]


def test_roll_pool_without_a_known_total_skips_the_sum_check() -> None:
    """`attribute_pool_total` 为 None（调用方没有这份权威总值）时跳过总和
    校验，同 `ruleset.attribute_point_buy` 为 None 时的处理——没有约束数据
    就没法裁决，不能瞎编一个值出来卡人。"""
    issues = validate_character(
        RULESET, ATTRS, ACCOUNTANT_NAME, {}, generation_method=GENERATION_ROLL_POOL
    )
    assert "ATTRIBUTE_POOL_MISMATCH" not in [issue.code for issue in issues]


def test_roll_pool_attribute_out_of_dice_range_is_rejected() -> None:
    """掷点池的单项区间是骰子公式本身的产出范围 [15, 90]，不是点数购买法的
    [10, 90]——15 以下不可能由 3d6*5/2d6+6*5 掷出来。"""
    issues = validate_character(
        RULESET,
        {**ATTRS, "STR": 10},
        ACCOUNTANT_NAME,
        {},
        generation_method=GENERATION_ROLL_POOL,
        attribute_pool_total=360,
    )
    assert "INVALID_ATTRIBUTES" in [issue.code for issue in issues]


def test_roll_pool_attribute_not_multiple_of_five_is_rejected() -> None:
    """掷点池分配的单项必须是 5 的倍数——骰子公式（3d6*5、(2d6+6)*5）的产出
    本来就只能是 5 的倍数，玩家手动分配不该凭空造出一个非法值。"""
    issues = validate_character(
        RULESET,
        {**ATTRS, "STR": 52},
        ACCOUNTANT_NAME,
        {},
        generation_method=GENERATION_ROLL_POOL,
        attribute_pool_total=402,
    )
    assert "INVALID_ATTRIBUTES" in [issue.code for issue in issues]


def test_age_outside_coc7_range_is_rejected() -> None:
    """COC7 的年龄档从 15-19 起、到 80-89 止，区间外要拒。

    前端此前把输入框写死成 [10, 100]，两头都不符合规则；现在区间由后端
    ruleset 声明并裁决。
    """
    from app.core.coc7_rules import validate_age

    assert [i.code for i in validate_age(RULESET, 10)] == ["INVALID_AGE"]
    assert [i.code for i in validate_age(RULESET, 90)] == ["INVALID_AGE"]
    assert validate_age(RULESET, 15) == []
    assert validate_age(RULESET, 89) == []


def test_age_not_filled_is_not_rejected() -> None:
    """年龄是本期才入库的字段，迁移前的卡都没有——不能拿新规则追溯判它们非法。"""
    from app.core.coc7_rules import validate_age

    assert validate_age(RULESET, None) == []


# ── issue #112：coc7_rules 改为参数注入，不再写死认识 COC7 ──────────────────


def test_compute_preview_works_with_a_minimal_non_coc7_ruleset() -> None:
    """规则核心必须只靠传入的 `RulesetRead` 就能算出结果，不依赖 `coc7_content`
    里的任何 COC7 具体数据——用一份跟 COC7 毫无关系的最小规则（2 项属性、
    2 条技能、1 个职业）跑通 `compute_preview`，证明这一点。"""
    minimal_ruleset = RulesetRead(
        attributes=[
            AttributeSpec(key="MIGHT", label="力量", generation="3d6*5"),
            AttributeSpec(key="WITS", label="智力", generation="3d6*5"),
        ],
        attribute_point_buy=None,
        age_range=None,
        skills=[
            SkillSpec(id="brawl", name="搏斗", base=20, category="combat"),
            SkillSpec(id="lore", name="见闻", base="WITS/2", category="knowledge"),
        ],
        occupations=[
            OccupationSpec(
                id=1,
                name="流浪者",
                category="misc",
                credit_min=0,
                credit_max=0,
                skill_points_formula="WITS*2",
                skill_ids=["brawl"],
                description="",
            )
        ],
    )

    result = compute_preview(
        minimal_ruleset,
        {"MIGHT": 40, "WITS": 60},
        1,
        {"brawl": 50},
    )

    assert result.validation == []
    # 职业技能点预算 = WITS*2 = 120，brawl 基础值 20、分配到 50 → 花费 30
    assert result.occupation_skill_points == SkillPointsBudget(budget=120, spent=30, remaining=90)
    # `_compute` 的兴趣点预算固定读 attributes 的 "INT" 键（COC7 遗留细节，
    # issue #112 不改变行为），minimal_ruleset 里没有这个属性，兜底为 0；
    # lore 没被分配点数（沿用基础值），兴趣花费也是 0。
    assert result.interest_skill_points == SkillPointsBudget(budget=0, spent=0, remaining=0)


def test_none_attribute_point_buy_and_age_range_skip_their_validations() -> None:
    """`ruleset.attribute_point_buy`/`ruleset.age_range` 为 `None`（自定义系统
    还没配置这两项约束）时，对应校验应该被跳过而不是崩溃或者拿 COC7 的默认值
    顶上——没有约束数据就没法裁决。"""
    ruleset_without_budget = RulesetRead(
        attributes=RULESET.attributes,
        attribute_point_buy=None,
        age_range=None,
        skills=RULESET.skills,
        occupations=RULESET.occupations,
    )

    # 点数购买法下，8 项可购买属性顶到 90（总和远超 COC7 的 480 预算），
    # 但没有 attribute_point_buy 数据可比，不应该报 ATTRIBUTE_POINTS_EXCEEDED。
    attrs = {**ATTRS, "STR": 90, "CON": 90, "POW": 90, "DEX": 90, "APP": 90}
    issues = validate_character(
        ruleset_without_budget, attrs, ACCOUNTANT_NAME, {}, generation_method="pointbuy"
    )
    assert "ATTRIBUTE_POINTS_EXCEEDED" not in [issue.code for issue in issues]

    # 年龄给一个 COC7 规则会拒绝的越界值（150），没有 age_range 数据可比，
    # 不应该报 INVALID_AGE。
    from app.core.coc7_rules import validate_age

    assert validate_age(ruleset_without_budget, 150) == []


def test_coc7_rules_module_does_not_import_coc7_content() -> None:
    """钉死 issue #112 的目标状态：规则核心不再直接依赖具体系统的规则数据
    模块，全部由调用方通过 `RulesetRead` 注入。

    查的是 **import 关系**（走 AST），不是源码里有没有出现这个词——注释和
    文档字符串本来就该能自由地提到 `coc7_content` 解释这段历史，用子串匹配
    会逼着文档绕开它把话说含糊。
    """
    import ast
    import pathlib

    import app.core.coc7_rules as coc7_rules_module

    tree = ast.parse(pathlib.Path(coc7_rules_module.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)

    assert not [name for name in imported if "coc7_content" in name]


# ── 职业技能自选槽（issue #114）────────────────────────────────────────


def _detective_with_slots() -> tuple[RulesetRead, OccupationSpec]:
    """借内置 COC7 规则数据，给私家侦探临时装上两个自选槽当夹具。

    槽的声明顺序**故意是「自由槽在前、社交槽在后」**——这是能逼出重排的顺序，
    见下面那条对拍用例的说明。

    ⚠️ 必须 `model_copy(deep=True)`：`build_coc7_ruleset()` 每次返回的是**同一批**
    `OccupationSpec` 对象（它直接引用 `coc7_content` 的模块常量，不做拷贝），
    直接改 `choice_slots` 会改到全局，把同一进程里其他用例一起带坏——第一版
    就是这么写的，一口气挂了 11 个不相干的测试。
    """
    ruleset = build_coc7_ruleset().model_copy(deep=True)
    occupation = next(o for o in ruleset.occupations if o.name == "私家侦探")
    occupation.choice_slots = [
        SkillChoiceSlot(count=2, candidate_skill_ids=None, label="任意两项特长"),
        SkillChoiceSlot(
            count=1,
            candidate_skill_ids=["charm", "fast-talk", "intimidate", "persuade"],
            label="一项社交技能",
        ),
    ]
    return ruleset, occupation


SLOT_ATTRS = {
    "STR": 50, "CON": 60, "POW": 55, "DEX": 45, "APP": 50,
    "SIZ": 60, "INT": 70, "EDU": 80, "LUCK": 50,
}  # fmt: skip


def test_choice_slot_skills_count_as_occupation_points() -> None:
    """占住自选槽的技能吃职业技能点，不是兴趣点。

    攀爬不在私家侦探的固定本职技能里，但「任意两项特长」这个槽收得下它。
    兴趣预算只有 INT*2=140，而这里非固定技能合计 150 点——如果槽没生效、
    全按兴趣点计费，就会触发 INTEREST_POINTS_EXCEEDED。
    """
    ruleset, _ = _detective_with_slots()

    issues = validate_character(
        ruleset,
        SLOT_ATTRS,
        "私家侦探",
        {"credit-rating": 25, "climb": 95, "swim": 95},
    )

    assert issues == []


def test_choice_slot_assignment_is_optimal_not_first_come_first_served() -> None:
    """🔴 占槽必须取「让职业技能覆盖最大」的分配，不能先来后到。

    合法性的判据是**存在**一种可行占槽方式，不是我们碰巧挑中的那种。这里
    persuade 社交槽和自由槽都收得下，climb/swim/ride 只有自由槽收得下，
    而自由槽只有 2 个：

    - 先来后到（按点数降序塞第一个能塞的槽）：persuade(80) 占掉自由槽①，
      ride(90) 占自由槽②，climb/swim 无处可去 → 未占槽 150 点 > 兴趣预算 140
      → 一张合法卡被 INTEREST_POINTS_EXCEEDED 判死。
    - 最优：persuade 让给社交槽，自由槽留给 ride(90)+climb(75) → 未占槽只剩
      swim 75 点 ≤ 140 → 合法。

    槽的声明顺序是「自由槽在前」，正是为了让先来后到的实现在这里出错——顺序
    反过来的话，persuade 先撞上社交槽，蒙对了，这条用例就失去区分力。
    """
    ruleset, _ = _detective_with_slots()

    issues = validate_character(
        ruleset,
        SLOT_ATTRS,
        "私家侦探",
        {"credit-rating": 25, "persuade": 90, "climb": 95, "swim": 95, "ride": 95},
    )

    assert issues == []


def test_skill_outside_every_slot_still_costs_interest_points() -> None:
    """槽是有限的：塞不进任何槽的技能照样吃兴趣点，超了要拦。

    跟上一条互为对照——上一条证明"该算职业点的别算成兴趣点"，这条证明
    "槽不是无限的免费通行证"。这里 4 项非固定技能合计 320 点，槽最多吸收 3 项，
    剩下的必然超 140。
    """
    ruleset, _ = _detective_with_slots()

    issues = validate_character(
        ruleset,
        SLOT_ATTRS,
        "私家侦探",
        {"credit-rating": 25, "climb": 95, "swim": 95, "ride": 95, "jump": 95},
    )

    assert [i.code for i in issues] == ["INTEREST_POINTS_EXCEEDED"]


def test_cthulhu_mythos_cannot_be_allocated_at_creation() -> None:
    """克苏鲁神话建卡时不可加点（issue #114）。

    规则依据（`COC7空白卡CY23Final.xlsx` 两处）：
    - `附表`：「没有调查员能在初始技能设定时给克苏鲁神话加点（除非被 KP 同意）」
    - `更新说明`：「兴趣技能点可以添加到任意技能(不包含克苏鲁神话)上」——即本
      项目已用作信用评级分账依据的那封 Chaosium 主编邮件

    修复前：给克苏鲁神话加 60 点，校验返回 0 条问题、直接放行。
    """
    ruleset = build_coc7_ruleset()

    issues = validate_character(
        ruleset, SLOT_ATTRS, "私家侦探", {"credit-rating": 25, "cthulhu-mythos": 60}
    )

    assert [i.code for i in issues] == ["SKILL_NOT_ALLOCATABLE"]


def test_cthulhu_mythos_at_base_value_is_fine() -> None:
    """基础值 0 本身合法——禁的是"加点"，不是"这项技能存在"。"""
    ruleset = build_coc7_ruleset()

    issues = validate_character(
        ruleset, SLOT_ATTRS, "私家侦探", {"credit-rating": 25, "cthulhu-mythos": 0}
    )

    assert issues == []


def test_private_investigator_rulebook_skills_not_rejected_as_interest() -> None:
    """🔴 issue #114 回归用例：一张按规则书合法、但在旧实现下被
    `INTEREST_POINTS_EXCEEDED` 误杀的私家侦探卡。

    旧的 30 项 curated 目录里，私家侦探的技能列表是移植时手工编的——被规整成
    恰好 8 项、且**漏了规则书里的图书馆使用和心理学**（塞进了汽车驾驶/格斗）。
    于是玩家把点数加在心理学 / 图书馆（规则书认可的本职技能）上时，会被当成
    兴趣技能计费而触发 `INTEREST_POINTS_EXCEEDED`。

    这里把 80 + 70 = 150 点加在这两项上，远超兴趣预算 100——如果它们仍被算作
    兴趣技能，就会被拒；只有当它们被正确识别为职业技能（吃 200 的职业预算），
    这张卡才合法。
    """
    ruleset = build_coc7_ruleset()

    # 心理学 base 10 → 90（+80）、图书馆 base 20 → 90（+70），信用取下限 9（职业点）
    skills = {"psychology": 90, "library-use": 90, "credit-rating": 9}
    issues = validate_character(ruleset, ATTRS, "私家侦探", skills)

    assert issues == [], "私家侦探把点加在规则书本职技能（心理学/图书馆）上应当合法"


# ── 分配值 vs 有效值（wizard-bugfix-round4，方案 A，#18/#20）───────────────
#
# 背景：`attributes` 存的是**有效值**（年龄修正之后的最终属性）；点数预算/
# 掷点池总和/步进为 5 这三条生成方法约束只对**分配值**（年龄修正之前）成立。
# 传 `allocated_attributes` 之后，校验改盯分配值，`attributes` 只做宽松兜底
# + 承担计算职责（衍生值/技能基础值/职业技能点公式）。

_POOL_KEYS = ["STR", "CON", "DEX", "APP", "POW", "SIZ", "INT", "EDU"]
_ALLOCATED_60 = {**ATTRS, **dict.fromkeys(_POOL_KEYS, 60)}  # 8 项各 60，总和 480


def test_allocated_attributes_lets_effective_values_skip_generation_method_checks() -> None:
    """#20 回归：掷点池角色年龄修正后，有效值（`attributes`）不再满足"总和
    精确等于池值 / 步进为 5"这两条只对分配值成立的约束——传
    `allocated_attributes` 之后校验改盯分配值，有效值只做宽松兜底，不再被
    整体拒绝；衍生值/两个技能点预算都基于**有效值**算出正常数字。"""
    # 模拟一次年龄修正的效果（40-49 档量级）：STR/CON/DEX 合计 -5、APP -5、
    # EDU 改进检定 +8——结果既不是 5 的倍数，总和也不再等于池值 480。
    effective = {**_ALLOCATED_60, "STR": 58, "CON": 58, "DEX": 59, "APP": 55, "EDU": 68}

    result = compute_preview(
        RULESET,
        effective,
        ACCOUNTANT_ID,
        {"credit-rating": 30},
        generation_method=GENERATION_ROLL_POOL,
        attribute_pool_total=480,
        allocated_attributes=_ALLOCATED_60,
    )

    assert result.validation == []
    assert result.derived_stats != {}
    # 职业技能点预算按**有效值** EDU=68 算（EDU*4=272），不是分配值 EDU=60
    # （240）——证明衍生计算确实用的是有效值，不是分配值。
    assert result.occupation_skill_points.budget == 272

    # 对照：不传 allocated_attributes 时，同样的有效值仍然会被判非法——证明
    # 上面那条测试真的在测新加的分支，不是碰巧过。
    without_allocation = compute_preview(
        RULESET,
        effective,
        ACCOUNTANT_ID,
        {"credit-rating": 30},
        generation_method=GENERATION_ROLL_POOL,
        attribute_pool_total=480,
    )
    codes = [issue.code for issue in without_allocation.validation]
    assert "INVALID_ATTRIBUTES" in codes or "ATTRIBUTE_POOL_MISMATCH" in codes


def test_allocated_attributes_does_not_bypass_effective_attribute_range_check() -> None:
    """越权兜底：即使分配值合法，有效值本身仍然要过"结构完整 + 落在 [1,99]"
    这道宽松校验——不能靠传一份合法分配值让越界的有效值蒙混过关。"""
    effective = {**_ALLOCATED_60, "STR": 999}

    result = compute_preview(
        RULESET,
        effective,
        ACCOUNTANT_ID,
        {},
        generation_method=GENERATION_ROLL_POOL,
        attribute_pool_total=480,
        allocated_attributes=_ALLOCATED_60,
    )

    assert "INVALID_ATTRIBUTES" in [issue.code for issue in result.validation]


def test_effective_attributes_implausible_deviation_is_rejected() -> None:
    """🔴 (d) 安全加固：宽松的 [1,99] 区间拦不住"分配值合法 + 有效值全部拉满
    99"这种客户端伪造——99 本身就落在 [1,99] 里。追加的总偏离上限应该拦住
    这种偏离量远超年龄修正理论上限（`max_total_adjustment_magnitude()`）的
    伪造数据。"""
    all_maxed = {**_ALLOCATED_60, **dict.fromkeys(_POOL_KEYS, 99)}

    result = compute_preview(
        RULESET,
        all_maxed,
        ACCOUNTANT_ID,
        {},
        generation_method=GENERATION_ROLL_POOL,
        attribute_pool_total=480,
        allocated_attributes=_ALLOCATED_60,
    )

    assert [issue.code for issue in result.validation] == ["EFFECTIVE_ATTRIBUTES_IMPLAUSIBLE"]


def test_effective_attributes_within_age_adjustment_magnitude_is_not_flagged() -> None:
    """对照：总偏离在 `max_total_adjustment_magnitude()` 以内（模拟真实年龄
    修正量级）不应该触发上面那条伪造检测。"""
    from app.core.coc7_age import max_total_adjustment_magnitude

    # STR/SIZ 各 -20、APP -15、EDU +30：总偏离 85，在上限（145，80-89 档最坏
    # 情形）以内。
    effective = {**_ALLOCATED_60, "STR": 40, "SIZ": 40, "APP": 45, "EDU": 90}
    total_delta = sum(abs(effective[k] - _ALLOCATED_60[k]) for k in _POOL_KEYS)
    assert total_delta <= max_total_adjustment_magnitude()

    result = compute_preview(
        RULESET,
        effective,
        ACCOUNTANT_ID,
        {"credit-rating": 30},
        generation_method=GENERATION_ROLL_POOL,
        attribute_pool_total=480,
        allocated_attributes=_ALLOCATED_60,
    )

    assert result.validation == []
