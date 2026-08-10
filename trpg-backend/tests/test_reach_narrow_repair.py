"""遭遇悬空要走窄修法，不能丢给整份重吐（`exec/31`，2026-08-10 真机拒绝后补）。

## 症状

真机导入被拒：`校验未通过：1 处问题（reach）`。门抓到的是**真缺陷**
（`mi-go-retreat` 没有入边），但接下来两轮自修**六次尝试全部失败**，
全断在同一位置：

    repair#1: attempt 1/3 failed: Unterminated string ... line 629
    repair#1: attempt 2/3 failed: Unterminated string ... line 629
    repair#1: attempt 3/3 failed: Expecting property name ... line 629
    repair#2: 同上三次

耗时 324 秒，最后仍是拒绝。

## 根因

`reach` 是跨实体错误（缺的边在**别的**节点上），于是被丢进了整份重吐——
而那条路在真实体量的模组上是**结构性失败**，`leak` 当初就是因此改成定向重写的。
**加了门却没给它配窄修法**，违反「只能成功，不能失败：遇到一类失败先找更窄的
修法，不是加一道门」。

## 修法

这条错误的作用域是所有类别里最小的——**缺的就是一个 id**。
所以只问一句「哪个节点该引出这一幕」，输出一行、结构上不可能截断，
再由纯代码把边写进 `leads_to`。
"""

from __future__ import annotations

from scripts.module_probe.assemble import (
    attach_encounter,
    dangling_encounter_ids,
    repair_module,
)


def _module() -> dict:
    return {
        "meta": {"id": "m", "title": "T"},
        "kp_truth": {"summary": "s", "key_facts": []},
        "player_intro": "intro",
        "nodes": [
            {"id": "living-room", "title": "客厅", "kp_text": "x", "leads_to": []},
            {
                "id": "mi-go-disguise",
                "title": "伪装",
                "kp_text": "x",
                "kind": "encounter",
                "leads_to": ["mi-go-retreat"],
            },
            {"id": "mi-go-retreat", "title": "撤退", "kp_text": "x", "kind": "encounter"},
        ],
    }


# ── 从错误里认出该接哪个节点 ──────────────────────────


def test_the_dangling_node_id_is_parsed_out_of_the_error() -> None:
    """窄修法的入口：得先知道是**哪一个**节点悬空。"""
    errors = [
        "遭遇节点 'mi-go-retreat' 没有任何入边（leads_to/exits/contains 都没人指向它），"
        "这一幕在对局里永远不会发生：请从触发它的调查点用 leads_to 指过来，"
        "或用 exits 接上它发生的地点"
    ]

    assert dangling_encounter_ids(errors) == ["mi-go-retreat"]


def test_ids_are_deduplicated_but_keep_order() -> None:
    """同一个节点可能被报多次（多轮自修累加），别修两遍。"""
    err = "遭遇节点 '{}' 没有任何入边（…）"

    ids = dangling_encounter_ids([err.format("b"), err.format("a"), err.format("b")])

    assert ids == ["b", "a"]


def test_unrelated_errors_are_ignored() -> None:
    assert dangling_encounter_ids(["[numeric] 数值对不上", "[leak] 真相关键词 'x' 出现在…"]) == []


# ── 接线是纯代码 ────────────────────────────────────


def test_attaching_writes_the_edge_into_the_parent() -> None:
    module = _module()

    assert attach_encounter(module, "living-room", "mi-go-retreat") is True
    assert module["nodes"][0]["leads_to"] == ["mi-go-retreat"]


def test_attaching_twice_does_not_duplicate_the_edge() -> None:
    module = _module()
    attach_encounter(module, "living-room", "mi-go-retreat")
    attach_encounter(module, "living-room", "mi-go-retreat")

    assert module["nodes"][0]["leads_to"] == ["mi-go-retreat"]


def test_a_self_loop_is_refused() -> None:
    """自环接不出可达性——它自己指向自己，玩家照样走不到。"""
    module = _module()

    assert attach_encounter(module, "mi-go-retreat", "mi-go-retreat") is False


def test_an_unknown_parent_is_refused() -> None:
    """父节点必须真的存在，否则会造出一条悬空引用（`check_refs` 会红）。"""
    module = _module()

    assert attach_encounter(module, "nowhere", "mi-go-retreat") is False


def test_it_can_attach_to_a_sub_node() -> None:
    """子节点也是合法的引出者——遭遇常常从某个房间里的物件触发。"""
    module = _module()
    module["nodes"][0]["sub_nodes"] = [{"id": "armchair", "title": "扶手椅", "kp_text": "x"}]

    assert attach_encounter(module, "armchair", "mi-go-retreat") is True
    assert module["nodes"][0]["sub_nodes"][0]["leads_to"] == ["mi-go-retreat"]


# ── 🔴 别再把它请回那条注定失败的路 ───────────────────


def test_the_whole_module_repairer_no_longer_instructs_on_reach() -> None:
    """整份重吐在真实体量上必断，所以 `reach` 不该出现在它的指示清单里。

    留着那段指示等于把这类错误请回一条**已经实测失败过**的路上——
    而且它会显得"已经处理了"，掩盖窄修法的缺失。
    """
    import inspect

    source = inspect.getsource(repair_module)

    assert "reach（遭遇节点没有入边）" not in source
    # 但要留下一句说明，否则下一个人会以为是漏了
    assert "repair_dangling_encounter" in source
