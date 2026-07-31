"""泄密守门（exec/14 P3）。

守的是**元层硬不变量**：`tier=meta` 的事实对任何虚构内主体永不可见，
出现在叙事正文里没有任何合法解释。

刻意**不碰** `tier=diegetic`——线索本来就是要在对局中揭示的（NPC 主动说、
守秘人判断时机成熟给出，都是合法主持），对它做"未挣得就拦"会疯狂误伤。
"""

from __future__ import annotations

from app.core.keeper.leak_guard import scan_meta_leaks, scrub_meta_leaks
from app.core.keeper.module_loader import (
    KeeperTruth,
    ModuleFact,
    ModuleMeta,
    ScenarioModule,
)

TRUTH = "道格拉斯并没有死而是被囚禁在地窖深处"
CLUE = "书桌抽屉的夹层里藏着一封没寄出的信"


def _module() -> ScenarioModule:
    return ScenarioModule(
        meta=ModuleMeta(id="m", title="合成模组"),
        kp_truth=KeeperTruth(summary="真相"),
        player_intro="开场",
        facts=[
            ModuleFact(id="fact-truth", text=TRUTH, kind="truth", tier="meta"),
            ModuleFact(id="fact-clue", text=CLUE, kind="clue", tier="diegetic"),
        ],
    )


# ── 硬拦：元层被逐字复述 ─────────────────────────────────────


def test_verbatim_meta_sentence_is_dropped() -> None:
    text = f"你推开门，屋里一片死寂。{TRUTH}。壁炉还残留着余温。"
    cleaned, hits = scrub_meta_leaks(text, _module())

    assert TRUTH not in cleaned
    assert "你推开门" in cleaned
    assert "壁炉还残留着余温" in cleaned
    assert [h.fact_id for h in hits] == ["fact-truth"]


def test_clean_narration_is_returned_unchanged() -> None:
    text = "管家站在门厅，双手交握。他说昨夜下过雨。"
    cleaned, hits = scrub_meta_leaks(text, _module())
    assert cleaned == text
    assert hits == []


# ── 不碰 diegetic：线索是拿来揭示的 ──────────────────────────


def test_diegetic_clue_is_never_scrubbed() -> None:
    """线索被写进叙事是合法主持，不是泄密——这条守门人不管它。"""
    text = f"你拉开抽屉。{CLUE}。"
    cleaned, hits = scrub_meta_leaks(text, _module())
    assert cleaned == text
    assert hits == []


# ── 部分复述也算：连续 14 字就够 ──────────────────────────────


def test_partial_verbatim_run_still_counts() -> None:
    """模型常复述半句而不是整条——半条也是泄密。"""
    half = TRUTH[:16]
    text = f"你忽然明白过来：{half}，只是没人敢说破。"
    cleaned, hits = scrub_meta_leaks(text, _module())
    assert half not in cleaned
    assert len(hits) == 1


def test_short_overlap_does_not_trigger() -> None:
    """短重合在合法叙事里必然撞车（人名地名），不能当判定。"""
    module = ScenarioModule(
        meta=ModuleMeta(id="m", title="t"),
        kp_truth=KeeperTruth(summary="s"),
        player_intro="p",
        facts=[ModuleFact(id="f", text="真凶是管家托马斯而不是园丁", kind="truth", tier="meta")],
    )
    # 只重合"管家托马斯"5 个字，远不到 14
    assert scan_meta_leaks("管家托马斯端来一杯热茶。", module) == []


def test_module_without_facts_is_a_noop() -> None:
    """尚未迁移的模组照常通过（向后兼容是硬要求）。"""
    module = ScenarioModule(
        meta=ModuleMeta(id="m", title="t"), kp_truth=KeeperTruth(summary="s"), player_intro="p"
    )
    text = "随便写点什么。"
    assert scrub_meta_leaks(text, module) == (text, [])


def test_only_the_offending_sentence_is_dropped_not_the_whole_paragraph() -> None:
    """整段丢弃会让叙事突然消失，玩家看到的是空气——只砍命中的那一句。"""
    text = f"第一句。{TRUTH}。第三句。第四句。"
    cleaned, _ = scrub_meta_leaks(text, _module())
    assert cleaned == "第一句。第三句。第四句。"


def test_fully_leaking_narration_never_falls_back_to_the_original() -> None:
    """🔴 整段都是泄密时的取舍与 prose_discipline 相反。

    那边砍空后退回原文（被砍的是格式问题，退回去顶多难看）；这边砍空后
    绝不退回原文——被砍的是模组真相，退回去就毁了整局。也不能返回空串，
    那样玩家看到的是空气。
    """
    cleaned, hits = scrub_meta_leaks(f"{TRUTH}。", _module())
    assert TRUTH not in cleaned
    assert cleaned.strip() != ""
    assert len(hits) == 1
