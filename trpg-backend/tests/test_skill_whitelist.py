"""检定 id 走白名单，不走别名表（`exec/30` 重排后的第 1 件事）。

## 为什么要改

`resolve_check_skill` 的 docstring 写着：别名表不算打地鼠，因为「输入是固定的
5 个模组文件」。**模组导入把那个前提拿掉了**——输入变成用户随手传的任何一份
PDF。真机实测当场撞到三种：

| 原文写法 | 为什么表里没有 |
|---|---|
| `电器维修` | 规则表里是「电气维修」，差一个字 |
| `踢` | 是「格斗：斗殴」下的一种动作，不是独立技能 |
| `INT×4` | 属性倍数检定，规则表里根本没有这个条目 |

判据（CLAUDE.md）：**不要用自由文本当标识符，解决它的是 enum**。所以做法不是
继续往别名表里加词，是在模型写的时候就让它从表里挑。

## 这一份守三件事

1. 发给模型的表 == `check_skills` 认的表（两份拷贝一定会漂）
2. 属性 id 在表里（否则 `INT×5` 这类检定没有合法落点，模型只能编）
3. 归一时**已经合法的 `skill_ids` 优先于展示名**——此前反过来，
   id 挑对了但中文名写岔一个字，正确的 id 会被擦掉
"""

from __future__ import annotations

from typing import Any

from scripts.module_probe.validate_module import (
    build_coc7_ruleset,
    check_skills,
    normalize_module_skills,
    render_skill_whitelist,
    skill_id_catalog,
)


def test_every_id_we_advertise_is_one_the_validator_accepts() -> None:
    """🔴 发给模型的表和校验用的表必须是同一份。

    不一致的症状最坏：模型照着我们给的表写，我们自己的校验说不认识——
    它改多少轮都过不去，而错的是我们。
    """
    ruleset = build_coc7_ruleset()
    catalog = skill_id_catalog(ruleset)
    advertised = {
        line.strip().split(" ", 1)[0]
        for line in render_skill_whitelist(ruleset).splitlines()
        if line.startswith("  ")
    }

    assert advertised, "白名单是空的"
    assert advertised == set(catalog), "白名单与 check_skills 的 id 表不一致"


def test_attribute_checks_have_a_legal_landing_spot() -> None:
    """`INT×5`（灵感）在 COC7 里是合法检定，而它不是技能。

    表里不给属性 id，模型就只能编一个技能名出来——真机上编的是 `INT×4`。
    """
    ruleset = build_coc7_ruleset()
    catalog = skill_id_catalog(ruleset)

    for attr in ruleset.attributes:
        assert attr.key in catalog, f"属性 {attr.key} 不在检定 id 表里"


def _one_check(**check: Any) -> dict[str, Any]:
    return {"nodes": [{"id": "n1", "checks": [check]}]}


def _first_check(raw: dict[str, Any]) -> dict[str, Any]:
    return raw["nodes"][0]["checks"][0]


def test_valid_ids_win_over_a_slightly_wrong_display_name() -> None:
    """🔴 这条正是「电器维修」那次失败。

    模型 id 挑对了（electrical-repair），中文名写成「电器维修」（表里是
    「电气维修」）。归一原本只认展示名并**覆盖** skill_ids，于是对的 id 被
    擦成空，`check_skills` 报「未归一到技能 id」——错的是归一，不是模型。
    """
    raw = _one_check(skill_ids=["electrical-repair"], skill="电器维修", kind="skill")

    normalize_module_skills(raw)

    check = _first_check(raw)
    assert check["skill_ids"] == ["electrical-repair"]
    assert check["skill"] == "电气维修", "展示名要从表里回填成规范写法"


def test_bad_ids_still_fall_back_to_resolving_the_name() -> None:
    """id 不在表里时不能盲信它，仍旧回到按名字解析这条老路。"""
    raw = _one_check(skill_ids=["not-a-real-skill"], skill="侦察", kind="skill")

    normalize_module_skills(raw)

    assert _first_check(raw)["skill_ids"] == ["spot-hidden"]


def test_normalized_module_passes_the_skill_gate() -> None:
    """端到端：挑对 id 的检定点能过 `check_skills` 这道硬门。"""
    import json
    from pathlib import Path

    from app.core.keeper.contract.module_loader import ScenarioModule

    fixture = Path(__file__).resolve().parent / "fixtures" / "keeper_module.json"
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    # 只替换检定点：其余字段用原创迷你剧本的真实形状，避免这条用例变成
    # 「我手搓的 dict 能不能过 pydantic」。
    raw["nodes"][0]["checks"] = [
        {"skill_ids": ["electrical-repair"], "skill": "电器维修", "kind": "skill"},
        {"skill_ids": ["INT"], "skill": "智力", "kind": "skill"},
    ]
    normalize_module_skills(raw)

    assert check_skills(ScenarioModule.model_validate(raw)) == []


# ── 属性×倍数是写法，不是同义词 ────────────────────────


def test_attribute_multiplier_forms_resolve_to_the_attribute() -> None:
    """🔴 真机连续撞到 `INT×4`。

    它不是"智力的另一种叫法"——是 COC 表达属性检定的**写法**（COC6 的灵感是
    INT×5、知识是 EDU×5，模组里什么倍数都写得出来）。往别名表里加 `INT×4`
    只挡得住这一个数字，下一份写 `INT×3` 又漏。所以做成规则。
    """
    from scripts.module_probe.validate_module import resolve_check_skill

    ruleset = build_coc7_ruleset()
    for writing in ("INT×4", "INT×5", "智力×5", "POW x 5", "EDU*5", "int×4"):
        kind, ids, _display = resolve_check_skill(writing, ruleset)
        assert kind == "skill", writing
        assert ids and ids[0] in {"INT", "POW", "EDU"}, f"{writing} 没解析成属性 id：{ids}"


def test_a_multiplier_on_something_that_is_not_an_attribute_is_not_guessed() -> None:
    """只有属性才吃这条规则。`侦察×2` 不是属性检定，猜它等于悄悄改了模组。"""
    from scripts.module_probe.validate_module import attribute_multiplier_check

    ruleset = build_coc7_ruleset()

    assert attribute_multiplier_check("侦察×2", ruleset) is None
    assert attribute_multiplier_check("智力", ruleset) is None, "没有倍数就不归这条规则管"


def test_the_multiplier_is_dropped_on_purpose() -> None:
    """倍数没有落点：本系统的难度走 SUCCESS_TIERS（÷2 / ÷5），没有"×N"这一档。

    `灵感 → INT` 早就是这么处理的，这里只是把同一件事推广到写法上。
    """
    from scripts.module_probe.validate_module import resolve_check_skill

    ruleset = build_coc7_ruleset()

    assert resolve_check_skill("INT×4", ruleset)[1] == resolve_check_skill("灵感", ruleset)[1]
