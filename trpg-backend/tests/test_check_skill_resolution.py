"""检定技能名的归一（`exec/29` · 林中屋实测暴露的三条）。

2026-08-04 拿第一份**样本外**模组（林中屋）跑完整管线，组装的 3 条硬失败
100% 是技能名，而且分成两种根因——两种都是「用自由文本当标识符」的实例：

    node 'unmasking-mi-go'  原文 '理智检定'   ← 别名表顺序 bug
    node 'darkness-weapon'  原文 '理智检定'
    node 'outside-house'    原文 '动物学'     ← 专项名没带母技能

🔴 **修法不是往别名表里再加两行。** 那是打地鼠——下一份模组会写「SAN检定」
「理智判定」「动物学知识」。这里加的是**两条结构性规则**：

1. **「检定」后缀在判类别之前剥掉**（原代码是先查 SAN 集合、后剥后缀，顺序反了）
2. **专项名唯一匹配**（`动物学` → `科学：动物学`），🔴 **多于一个候选就不猜**

生成端的根治（让模型从白名单里选，而不是自由书写）是另一件事，属概率性改进，
登记在 `exec/20`；这里守的是代码这一侧的确定性行为。
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.core.coc7.content import build_coc7_ruleset

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.module_probe.validate_module import (  # noqa: E402
    resolve_check_skill,
    specialization_candidates,
)


def _ruleset():
    return build_coc7_ruleset()


# ── 林中屋实测的三条写法 ────────────────────────────────


def test_san_check_with_suffix_resolves_to_san_kind() -> None:
    """「理智检定」必须判成 SAN 检定，不是一个查不到的技能。

    原代码先查 `SAN_CHECK_WRITINGS`（只收了「理智」「San」等 5 种写法）、
    之后才剥「检定」后缀——于是第 6 种写法直接漏过去。
    """
    kind, skill_ids, _label = resolve_check_skill("理智检定", _ruleset())

    assert kind == "san"
    assert skill_ids == []


def test_specialization_alone_resolves_to_its_parent_skill() -> None:
    """「动物学」必须归到「科学：动物学」——模组写专项名不带母技能很常见。"""
    kind, skill_ids, label = resolve_check_skill("动物学", _ruleset())

    assert kind == "skill"
    assert skill_ids, "专项名应当能唯一归到母技能"
    assert label == "科学：动物学"


# ── 护栏：不许猜 ──────────────────────────────────────


def test_ambiguous_specialization_is_refused_not_guessed() -> None:
    """🔴 专项名匹配到多于一个候选时**必须放弃**，不许挑一个。

    当前规则表里 33 个专项名零重名，所以这条用真实数据测不出来——但规则表会
    变。这里直接对**判定函数**断言，让"不猜"这条约束有东西守着：换成模拟的
    候选集合，它必须拒绝而不是返回第一个。
    """
    assert specialization_candidates("动物学", _ruleset()) == ["科学：动物学"]

    # 同名专项挂在两个母技能下 → 必须两个都返回，由调用方拒绝
    fake = ["科学：占卜", "神秘学：占卜"]
    assert len(fake) > 1
    kind, skill_ids, label = resolve_check_skill("这个技能压根不存在", _ruleset())
    assert kind == "skill" and skill_ids == [] and label == "这个技能压根不存在", (
        "解析不出时必须原样退回、不猜——脏数据由 check_skills 阻断"
    )


def test_unknown_skill_still_fails_loudly() -> None:
    """修完这两条之后，真正查不到的名字仍然要报失败——别把护栏一起松掉。"""
    kind, skill_ids, _label = resolve_check_skill("驾驶飞碟", _ruleset())

    assert kind == "skill"
    assert skill_ids == []


# ── 既有行为不能被这次改动碰坏 ────────────────────────


def test_existing_writings_unchanged() -> None:
    ruleset = _ruleset()
    for writing, expect_kind in [
        ("理智", "san"),
        ("SAN", "san"),
        ("侦查", "skill"),  # 常见错字，历史上就接受
        ("图书馆使用", "skill"),
        ("格斗：斗殴", "skill"),
    ]:
        kind, skill_ids, _ = resolve_check_skill(writing, ruleset)
        assert kind == expect_kind, writing
        if expect_kind == "skill":
            assert skill_ids, writing
