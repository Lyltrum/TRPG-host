"""预设调查员卡有了自己的归宿（`exec/30` 重排后的第 2 件事）。

## 症状

复足实测被拒绝，理由是：

    [thin_slot] 薄公开槽 player_intro 归入 5 个片段（上限 1）：pregen-…×5

归组模型给出的理由是「预制角色，玩家可见」——**这句话没错**。错的是我们：
schema 里没有预设调查员卡的位置（本系统的角色由玩家自己建），它无处可去，
模型就近塞进了最像的那个薄槽，五张卡挤进一个"最多 1 个"的位置。

同族于「schema 表达不了的东西会从别处漏出去」：看到模型往奇怪的地方塞，
先查 schema 是不是缺了口子，再考虑加 prompt 约束。

## 修法与它的边界

给它一个显式的名字 `pregen`，然后**显式降级**：不进 structured，但清点并
告诉用户。**不做的是**真把预设卡导进来当可选角色——那是一整条产品能力。
"""

from __future__ import annotations

from scripts.module_probe.validate_module import (
    OUT_OF_SCOPE_KINDS,
    check_content_preservation,
    check_thin_public_slots,
    count_out_of_scope,
)


def _assignments(kind: str, n: int) -> dict[str, dict[str, str]]:
    return {
        f"pregen-{i}": {"dest_kind": kind, "dest_id": "pregen", "reason": "预制角色"}
        for i in range(n)
    }


def test_five_pregen_cards_in_a_thin_slot_is_still_rejected() -> None:
    """先钉住原症状：塞进薄槽必须继续拒绝，这条门不能因为新归宿而松掉。"""
    errors = check_thin_public_slots(_assignments("player_intro", 5))

    assert any("player_intro" in e for e in errors)


def test_the_new_home_does_not_trip_the_thin_slot_gate() -> None:
    """归到 pregen 之后，同样五张卡不再撞薄槽上限。"""
    assert check_thin_public_slots(_assignments("pregen", 5)) == []


def test_they_are_counted_not_silently_dropped() -> None:
    """🔴 丢弃必须留下数字。

    静默丢掉和"这份模组本来就没有预设卡"看起来一模一样，而两者对用户的意义
    完全不同——他可能正指望用那几张卡开局。
    """
    assert count_out_of_scope(_assignments("pregen", 5)) == {"pregen": 5}
    assert count_out_of_scope(_assignments("node", 5)) == {}


def test_out_of_scope_is_not_reported_as_lost_content() -> None:
    """内容保全那道软项不该把它当成"内容蒸发"——它本来就不进 structured。

    不豁免的话，每份带预设卡的模组都会多出几条噪声，而真正的内容丢失就被
    埋在噪声里了。
    """
    assignments = _assignments("pregen", 3)
    items = [{"id": iid, "summary": "一张预设调查员卡"} for iid in assignments]

    suspects = check_content_preservation(items, assignments, None, {})

    assert [s for s in suspects if s.dest_kind in OUT_OF_SCOPE_KINDS] == []


# ── 🔴 归宿与说明必须成对 ─────────────────────────────


def test_every_out_of_scope_kind_has_something_to_say() -> None:
    """加一类「用不上」的归宿，就得配一句告诉用户的话。

    漏了那一句，那类材料就从"显式降级"退回"静默丢弃"——而且什么都不会红。
    同族于「加了新的失败类别，要同步更新每一个逐个列出类别的消费方」。
    """
    from scripts.module_probe.pipeline import _OUT_OF_SCOPE_NOTICES

    assert set(_OUT_OF_SCOPE_NOTICES) == set(OUT_OF_SCOPE_KINDS)
    for kind, sentence in _OUT_OF_SCOPE_NOTICES.items():
        assert "{count}" in sentence, f"{kind} 那句话没报数量"


def test_front_matter_is_the_second_case_of_the_same_disease() -> None:
    """目录/版权页/译者说明看起来最像 meta，八段一起挤进上限 1 的薄槽。

    真机实测：回灌归组也修不好，因为它还是没别的地方可放。
    """
    front = {
        f"fm-{i}": {"dest_kind": "front_matter", "dest_id": "front", "reason": "附页"}
        for i in range(8)
    }

    assert check_thin_public_slots(front) == []
    assert count_out_of_scope(front) == {"front_matter": 8}
