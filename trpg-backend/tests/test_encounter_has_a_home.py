"""遭遇的归宿是靠 prompt 里的定义守住的，所以定义本身要有测试盯着。

`exec/30 §9` 的主修法不是那道门，是**把 `npc` 的定义收窄、给遭遇一个落点**。
它活在 prompt 文本里，没有任何类型系统或校验会在它被改回去时变红——
下一个人一句「npc：人物或怪物」就能把整件事退回原状。

同族于「架构约束必须有测试守护，否则一定退化」，做法照 `test_pregen_out_of_scope`
里那条「归宿与说明必须成对」。
"""

from __future__ import annotations

import inspect

from app.core.module_import.job_state import FAILURE_KINDS
from scripts.module_probe.assemble import (
    REACH_REPAIR_SYSTEM,
    STAGE1_SYSTEM,
    STAGE2_NPC_SYSTEM,
    TARGET_SCHEMA_DOC,
    repair_dangling_encounter,
    repair_module,
)
from scripts.module_probe.validate_module import ENCOUNTER_KIND, ValidationReport


def test_grouping_stage_forbids_encounters_from_landing_in_npc() -> None:
    """归组阶段必须明说「遭遇不归 npc」。

    坏那次归组模型给的理由是「这是关于那只怪物的」——**那句话没错**，错的是
    我们没给它别的地方放。
    """
    assert "不承载场景" in STAGE1_SYSTEM
    assert "不许归进 npc" in STAGE1_SYSTEM


def test_grouping_stage_names_the_destination_and_the_test_for_it() -> None:
    """光说"不许"不够，得说清"那该去哪"，以及怎么判。"""
    assert ENCOUNTER_KIND in STAGE1_SYSTEM
    # 判据要在场：分不清就还是会塞回 npc
    assert "会发生的一件事" in STAGE1_SYSTEM


def test_npc_forming_stage_keeps_kp_notes_a_character_sketch() -> None:
    """`kp_notes` 收窄是修法的另一半——不收窄，它还是那个什么都装得下的袋子。"""
    assert "人物小传，不是剧本" in STAGE2_NPC_SYSTEM


def test_schema_doc_documents_the_encounter_kind() -> None:
    """模型是照 schema 文档写的；文档里没有这个 kind，它就不会用。"""
    assert ENCOUNTER_KIND in TARGET_SCHEMA_DOC


def test_this_category_has_a_repair_path_and_it_is_the_narrow_one() -> None:
    """🔴 加了一类失败就要给它配一条**走得通**的修法。

    trace / numeric 两类曾经漏掉自修指示，于是**永远修不掉、什么都不会变红**，
    只是拒绝率悄悄变高。`reach` 一开始补上了指示，但补错了地方——补进了
    **整份重吐**，而那条路在真实体量的模组上是结构性失败。
    2026-08-10 真机实测：两轮自修六次尝试全断在同一位置，324 秒后仍是拒绝。

    所以这条用例守的不是"有没有写指示"，是**指示写在走得通的那条路上**：
    `reach` 归 `repair_dangling_encounter`（只回一个 id，输出有界），
    **不归** `repair_module`。
    """
    whole = inspect.getsource(repair_module)

    # 整份重吐那条路不该再认领它
    assert "reach（遭遇节点没有入边）" not in whole
    # 但要留下一句指路，否则下一个人会以为是漏了
    assert "repair_dangling_encounter" in whole

    # 窄修法必须只要一个 id（输出有界才不会被截断），且挑不出时不许硬凑
    assert '{"from": "<节点 id>"}' in REACH_REPAIR_SYSTEM
    assert "不要硬凑" in REACH_REPAIR_SYSTEM
    assert repair_dangling_encounter.__doc__ is not None


def test_the_new_category_can_cross_to_the_frontend() -> None:
    """类别词是唯一被允许跨到前端的东西（错误原文里有剧透）。

    不在这个封闭集合里的前缀会被 `normalize_failure_kinds` 静默丢掉——
    于是前端只看到"校验未通过 0 处问题"，用户完全不知道发生了什么。
    """
    assert "reach" in FAILURE_KINDS


def test_every_report_prefix_is_a_known_failure_kind() -> None:
    """报告里能出现的每个方括号前缀，都必须在封闭集合里有对应。

    这条是上一条的一般化：它在**下一次**有人加门却忘了登记时也会红。
    """
    report = ValidationReport(
        ok=False,
        schema_ok=False,
        schema_errors=["e"],
        ref_errors=["e"],
        skill_errors=["e"],
        orphan_errors=["e"],
        leak_errors=["e"],
        facts_errors=["e"],
        thin_slot_errors=["e"],
        secret_public_errors=["e"],
        structure_errors=["e"],
        trace_errors=["e"],
        numeric_errors=["e"],
        reach_errors=["e"],
    )

    prefixes = {e.split("]")[0].lstrip("[") for e in report.all_errors()}

    assert prefixes == set(FAILURE_KINDS)
