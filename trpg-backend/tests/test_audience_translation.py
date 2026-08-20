"""受众翻译层（2026-08-20）—— 自由文本 → 枚举，代码只认枚举。

## 🔴 为什么会有这一层

抽取那头**刻意**让 audience 保持自由文本（`probe.py`：常见是玩家可见或 KP
绝密，"但也可能是别的情况，按内容如实写，不要强迫二选一"）。那是对的：实测里
`玩家可见（守秘人笔记部分为守秘人绝密）` 这种复合受众，二选一确实会丢信息。

**错在下游没有翻译就直接拿它当标识符用。** 原来的判定是个关键词表
（`"绝密" / "守密人" / "KP"`），而五份模组实测有 20 多种写法，那张表同时有
漏判和误判：

- **漏**：表里写「守密**人**」，数据里是「守**秘**人」，一字之差全漏；
- **误**：`玩家可见（守秘人笔记部分为守秘人绝密）` 含"绝密"就被整条判成 KP。

判据：**不要用自由文本当标识符，要么是白名单 id，要么退化成同义词打地鼠。**
"""

from __future__ import annotations

import json
from typing import Any

from scripts.module_probe.assemble import (
    AUDIENCE_KINDS,
    CallStats,
    apply_audience_kinds,
    enforce_audience_slots,
    translate_audiences,
)


class _FakeCompletions:
    def __init__(self, mapping: dict[str, str] | None) -> None:
        self._mapping = mapping
        self.prompts: list[str] = []

    def create(self, **kwargs: Any):
        self.prompts.append(kwargs["messages"][1]["content"])
        payload = {} if self._mapping is None else {"kinds": self._mapping}

        class _Msg:
            content = json.dumps(payload, ensure_ascii=False)

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]
            usage = None

        return _Resp()


class _FakeClient:
    def __init__(self, mapping: dict[str, str] | None) -> None:
        self.chat = type("_C", (), {"completions": _FakeCompletions(mapping)})()

    @property
    def prompts(self) -> list[str]:
        return self.chat.completions.prompts


#: 真实取值，从五份模组的裸抽取里量出来的（含那两个坑）
_REAL_WRITINGS = [
    "玩家可见",
    "KP绝密",
    "KP 绝密",
    "守秘人绝密",
    "守秘人",
    "KP（模组真相部分）",
    "玩家可见（守秘人笔记部分为守秘人绝密）",
]


def _items(audiences: list[str]) -> list[dict[str, Any]]:
    return [{"id": f"it-{i}", "audience": a} for i, a in enumerate(audiences)]


def test_identical_writings_cost_only_one_translation() -> None:
    """🔴 **按字符串去重**：170 个片段都写"玩家可见"，只该翻译一次。

    不去重的话，一份 241 片段的模组要翻 241 条，而实测去重后只有 10–20 种。
    """
    client = _FakeClient({"玩家可见": "player"})
    result = translate_audiences(
        client,  # ty: ignore[invalid-argument-type]
        _items(["玩家可见"] * 50),
        CallStats(),
    )

    assert len(client.prompts) == 1
    assert client.prompts[0].count("玩家可见") == 1, "同一种写法在 prompt 里出现了多次"
    assert result == {"玩家可见": "player"}


def test_an_empty_audience_costs_nothing() -> None:
    """空值没有内容可理解，不该花一次调用。"""
    client = _FakeClient({})
    empty = translate_audiences(
        client,  # ty: ignore[invalid-argument-type]
        _items(["", "  "]),
        CallStats(),
    )
    assert empty == {}
    assert client.prompts == []


def test_every_writing_gets_a_kind_even_if_the_model_skips_it() -> None:
    """🔴 模型漏答或答了非法值时**落到 kp**，不是 player。

    判错的代价不对称：多藏一段只是主持人多讲一句，剧透一次毁掉整局。

    **变异检验**：把兜底改成 `"player"`，这条当场红。
    """
    client = _FakeClient({"玩家可见": "player", "KP绝密": "外星人"})  # 第二条是非法值
    result = translate_audiences(
        client,  # ty: ignore[invalid-argument-type]
        _items(["玩家可见", "KP绝密", "守秘人"]),
        CallStats(),
    )

    assert result["玩家可见"] == "player"
    assert result["KP绝密"] == "kp", "非法值没落到 kp"
    assert result["守秘人"] == "kp", "漏答的没落到 kp"
    assert set(result.values()) <= set(AUDIENCE_KINDS)


def test_a_missing_audience_becomes_kp_on_the_item() -> None:
    """片段上也要有值：没有 audience 的片段按最保守的来。"""
    items = _items(["玩家可见", ""])
    apply_audience_kinds(items, {"玩家可见": "player"})
    assert items[0]["audience_kind"] == "player"
    assert items[1]["audience_kind"] == "kp"


# ── 确定性修补：KP 片段不许留在公开槽 ──────────────────────


def _amap(pairs: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    return {iid: {"dest_kind": kind, "dest_id": "x", "reason": ""} for iid, kind in pairs}


def test_a_kp_item_is_moved_out_of_a_public_slot() -> None:
    """🔴 **这一步必须是代码，不是重试**。

    真机实测：一个叫 `introduction` 的片段，audience 是 `KP（模组真相部分）`，
    归组模型**每一轮都把它归进 opening**——那是它的语义直觉，重试三次一模一样。
    而这一条错误让 `needs_stage1_repair()` 永远为真，整个自修被锁死在"整份重吐"
    那条路上，后面所有窄路一次都没跑过。

    **变异检验**：把 `enforce_audience_slots` 改成只 return 不改，这条当场红。
    """
    amap = _amap([("introduction", "opening"), ("intro-2", "player_intro")])
    items = {
        "introduction": {"audience_kind": "kp"},
        "intro-2": {"audience_kind": "kp"},
    }

    moved = enforce_audience_slots(amap, items)

    assert len(moved) == 2
    assert amap["introduction"]["dest_kind"] == "kp_guidance"
    assert amap["intro-2"]["dest_kind"] == "kp_guidance"
    # 🔴 不是 kp_truth：被误归进 opening 的通常是「模组简介」这类主持指引，
    # 放成谜底会让收尾判据把它当真相侧
    assert "kp_truth" not in {v["dest_kind"] for v in amap.values()}


def test_player_and_both_items_are_left_alone() -> None:
    """🔴 `both` 不算绝密——主体是玩家可见，只是夹着一段主持人笔记。

    这正是旧关键词表误判的那一类（`玩家可见（守秘人笔记部分为守秘人绝密）`
    含"绝密"就被整条拦下）。**变异检验**：把判断改成 `kind != "player"`，
    这条当场红。
    """
    amap = _amap([("a", "opening"), ("b", "player_intro")])
    items = {"a": {"audience_kind": "player"}, "b": {"audience_kind": "both"}}

    assert enforce_audience_slots(amap, items) == []
    assert amap["a"]["dest_kind"] == "opening"
    assert amap["b"]["dest_kind"] == "player_intro"


def test_kp_items_outside_public_slots_are_not_touched() -> None:
    """KP 片段本来就在 KP 槽里的，别动它。"""
    amap = _amap([("a", "kp_truth"), ("b", "node")])
    items = {"a": {"audience_kind": "kp"}, "b": {"audience_kind": "kp"}}

    assert enforce_audience_slots(amap, items) == []
    assert amap["a"]["dest_kind"] == "kp_truth"
    assert amap["b"]["dest_kind"] == "node"


def test_the_old_keyword_table_would_have_failed_on_these() -> None:
    """🔴 **回归钉子**：把旧关键词表挂过的两条真实写法钉死。

    旧实现是 `any(sig in a for sig in ("绝密", "守密人", "KP", "kp"))`。
    这条测试不调用它（它已经不存在了），而是断言**新实现在这两条上给出正确
    结果**——旧表在这两条上一个漏一个误。
    """
    # ① 旧表漏判：「守秘人」不含"绝密"、不含"守密人"、不含"KP"
    amap = _amap([("leak", "opening")])
    assert enforce_audience_slots(amap, {"leak": {"audience_kind": "kp"}})
    assert amap["leak"]["dest_kind"] == "kp_guidance"

    # ② 旧表误判：主体玩家可见、只夹了一段绝密
    amap2 = _amap([("mixed", "opening")])
    assert enforce_audience_slots(amap2, {"mixed": {"audience_kind": "both"}}) == []
    assert amap2["mixed"]["dest_kind"] == "opening"


def test_the_prompt_shows_the_model_the_real_writings() -> None:
    """翻译 prompt 里要能看到全部原始写法——它是唯一的判断依据。"""
    client = _FakeClient(dict.fromkeys(_REAL_WRITINGS, "kp"))
    translate_audiences(
        client,  # ty: ignore[invalid-argument-type]
        _items(_REAL_WRITINGS),
        CallStats(),
    )

    prompt = client.prompts[0]
    for writing in _REAL_WRITINGS:
        assert writing in prompt


# ── 无解 check 直接扔掉（2026-08-20）──────────────────────


def test_a_check_with_no_resolvable_skill_is_dropped_not_fatal() -> None:
    """🔴 真机：`checks[2] 未归一到技能 id（原文 ''）`——**模型吐了个空技能名**。

    那条 check 里没有任何可用信息，留着 = 整份模组作废，扔掉 = 少一个检定点、
    那一幕照样能主持。定位是「只能成功不能失败，遇到失败先找更窄的修法」，
    一个空 check 正是最该被窄化掉的东西。

    **变异检验**：把 `drop_unresolvable_checks` 改成只 return 不删，这条当场红。
    """
    from scripts.module_probe.validate_module import drop_unresolvable_checks

    module = {
        "nodes": [
            {
                "id": "glow-heartbeat",
                "checks": [
                    {"kind": "skill", "skill": "侦察", "skill_ids": ["spot-hidden"]},
                    {"kind": "skill", "skill": "", "skill_ids": []},
                ],
            }
        ]
    }

    dropped = drop_unresolvable_checks(module)

    assert len(dropped) == 1
    assert "glow-heartbeat" in dropped[0]
    kept = module["nodes"][0]["checks"]
    assert len(kept) == 1 and kept[0]["skill_ids"] == ["spot-hidden"]


def test_a_san_check_is_never_dropped() -> None:
    """🔴 理智检定本来就不指向技能，扔了会真的丢东西。

    `check_skills` 自己也跳过 kind=san。**变异检验**：去掉那个 san 判断，
    这条当场红。
    """
    from scripts.module_probe.validate_module import drop_unresolvable_checks

    module = {"nodes": [{"id": "n1", "checks": [{"kind": "san", "skill": "", "skill_ids": []}]}]}

    assert drop_unresolvable_checks(module) == []
    assert len(module["nodes"][0]["checks"]) == 1


def test_it_reaches_nested_nodes() -> None:
    """子节点里的空 check 一样要扔——树形结构漏一层就等于没做。"""
    from scripts.module_probe.validate_module import drop_unresolvable_checks

    module = {
        "nodes": [
            {
                "id": "top",
                "checks": [],
                "sub_nodes": [{"id": "deep", "checks": [{"kind": "skill", "skill_ids": []}]}],
            }
        ]
    }

    assert len(drop_unresolvable_checks(module)) == 1
    assert module["nodes"][0]["sub_nodes"][0]["checks"] == []


def test_normalise_runs_before_pruning() -> None:
    """🔴 顺序不能反：先救「电器维修」这种一字之差的，再扔真的无解的。

    反过来的话，一个还有救的 check 会先被当成无解扔掉。
    """
    from scripts.module_probe.validate_module import normalize_and_prune_checks

    module = {
        "nodes": [{"id": "n1", "checks": [{"kind": "skill", "skill": "侦查", "skill_ids": []}]}]
    }

    normalize_and_prune_checks(module)

    kept = module["nodes"][0]["checks"]
    assert len(kept) == 1, "「侦查」是能归一到 spot-hidden 的，不该被扔"
    assert kept[0]["skill_ids"] == ["spot-hidden"]


def test_every_json_mode_prompt_says_the_word_json() -> None:
    """🔴 DeepSeek 硬性要求：用 `response_format={"type":"json_object"}` 时，
    **prompt 里必须出现字面的 "json"**，否则 400。

    真机上就是这么挂的：`AUDIENCE_SYSTEM` 里只有 `{"kinds": {...}}` 的形状，
    没有那个词，模型侧直接 400，重试 3 次全挂——而本文件 13 条测试当时全绿，
    因为替身 client 不检查这条约束。

    **这条约束是"逐个列出的地方，加一项就漏一项"的典型**：以后每加一个走
    `_chat_json` 的 prompt 都要记得，而没人会记得。所以这里**扫前缀**——
    模块里所有 `*_SYSTEM` 常量一个不漏。

    **变异检验**：把任意一个 prompt 里的 "JSON" 删掉，这条当场红。
    """
    from scripts.module_probe import assemble, probe, relation_probe

    missing: list[str] = []
    for module in (assemble, relation_probe, probe):
        for name in dir(module):
            if not name.endswith("_SYSTEM"):
                continue
            prompt = getattr(module, name)
            if isinstance(prompt, str) and "json" not in prompt.lower():
                missing.append(f"{module.__name__.split('.')[-1]}.{name}")

    assert not missing, f"这些 prompt 走 JSON mode 却没提到 json，模型侧会直接 400：{missing}"
