"""归组分批（2026-08-20）—— `stage1_group` 的输出长度不许再正比于片段数。

## 🔴 为什么会有这个文件

真机导入一份 **241 片段**的模组，`stage1.group` 写到 **24,523 字符**仍未写完，
JSON 被截断（`Unterminated string starting at: line 1042 column 7`），自动重试
3 次全部同样失败。历史最大的一份是 116 片段（跑通过），阈值落在两者之间。

根因不是超时、不是崩溃、不是切歪，是 **stage1 要求「每个片段 id 必须出现在
assignments 中」⇒ 输出 ∝ 片段数，而 `max_tokens` 是常数**。

🔴 **调大 `max_tokens` 不是修法**（那是把阈值往后挪一格，下一份更碎的模组照撞）。
隔壁 `relation_probe` 早就是「总是分批」，只有归组这步没照做。

## 这里只测代码那一半

「归组质量会不会因为分批变差」是语义问题，单测答不了，归 `exec/20` 的概率性
改进，靠真机看。这里守的是**接线与不变量**：片段不许因为分批丢掉、实体不许
因为分批裂成两个、批外信息不许缺席。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from scripts.module_probe.assemble import STAGE1_BATCH_SIZE, CallStats, stage1_group


class _FakeCompletions:
    """替身：按批产出「本批片段各归一个实体」的合法结果，并记下每次的 prompt。"""

    def __init__(self, entity_id_for) -> None:
        self._entity_id_for = entity_id_for
        self.prompts: list[str] = []
        self.labels: list[str] = []

    def create(self, **kwargs: Any):
        user = kwargs["messages"][1]["content"]
        self.prompts.append(user)
        self.labels.append(kwargs.get("tape_key") or "")

        # 从「本批要归组的片段」那一段里把 id 抠出来
        marker = "【本批要归组的片段"
        body = user[user.index(marker) :]
        ids = [
            line.split("id: ", 1)[1].strip()
            for line in body.splitlines()
            if line.startswith("- id: ")
        ]
        entities: dict[str, list[str]] = {}
        assignments = []
        for iid in ids:
            eid = self._entity_id_for(iid)
            entities.setdefault(eid, []).append(iid)
            assignments.append(
                {"item_id": iid, "dest_kind": "node", "dest_id": eid, "reason": "测试"}
            )
        payload = {
            "entities": [
                {"kind": "node", "id": eid, "title": f"场景 {eid}", "item_ids": iids}
                for eid, iids in entities.items()
            ],
            "assignments": assignments,
            "orphans": [],
        }

        class _Msg:
            content = json.dumps(payload, ensure_ascii=False)

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]
            usage = None

        return _Resp()


class _FakeClient:
    def __init__(self, entity_id_for) -> None:
        self.chat = type("_Chat", (), {"completions": _FakeCompletions(entity_id_for)})()

    @property
    def prompts(self) -> list[str]:
        return self.chat.completions.prompts

    @property
    def labels(self) -> list[str]:
        return self.chat.completions.labels


def _items(n: int) -> list[dict[str, Any]]:
    return [
        {"id": f"it-{i}", "title": f"片段{i}", "what_kind_of_thing": "描述", "summary": "…"}
        for i in range(n)
    ]


def _run(n: int, entity_id_for=lambda iid: "loc-1", batch_size: int = STAGE1_BATCH_SIZE):
    client = _FakeClient(entity_id_for)
    # 替身只需要 `chat.completions.create`，不实现整个 TapedSyncClient
    result = stage1_group(
        client,  # ty: ignore[invalid-argument-type]
        _items(n),
        [],
        CallStats(),
        batch_size=batch_size,
    )
    return client, result


def test_no_item_is_lost_when_batching() -> None:
    """🔴 **核心不变量**：分几批都好，每个片段都得有归宿。

    `_normalize_stage1` 会给漏掉的片段机械补 `unassigned`——所以只断言"键齐全"
    是不够的，那条补洞逻辑会让任何漏掉都看起来正常。这里同时断言**没有一条是
    补出来的**。
    """
    _, result = _run(241, batch_size=60)
    amap = result["assignment_map"]

    assert len(amap) == 241
    assert {f"it-{i}" for i in range(241)} == set(amap)
    patched = [k for k, v in amap.items() if v["dest_kind"] == "unassigned"]
    assert not patched, f"这些片段是被补洞逻辑救回来的，说明分批把它们漏了：{patched[:5]}"


def test_it_really_splits_into_batches() -> None:
    """自证：装置真的走到了多批那条路（否则上面那条测的是全量路径）。"""
    client, _ = _run(241, batch_size=60)
    assert len(client.prompts) == 5, "241 / 60 应该是 5 批"


def test_one_batch_is_indistinguishable_from_before() -> None:
    """退化保证：片段少的模组只有一批，行为与分批前一致。

    包括**磁带键**——只有一批时 label 仍是 `stage1.group`，不带批号，否则
    既有磁带全部作废。
    """
    client, result = _run(50, batch_size=60)
    assert len(client.prompts) == 1
    assert len(result["assignment_map"]) == 50
    assert "batch" not in client.labels[0], "单批不该带批号，会让既有磁带失效"


def test_the_tape_key_scheme_already_separates_the_batches() -> None:
    """🔴 **这里不需要一条"每批键不同"的断言**，而我先写了一条，它永远绿。

    `tape_key_for` 自带出现序号（`label` / `label#2` / `label#3`…），所以哪怕
    几批共用同一个 label，磁带键也不会撞——那条断言无论代码怎么改都通过，
    是虚假的安全感（同「守护测试自己会瞎掉」那族）。

    真正要守的是**另一头**：批号只在多批时出现，单批必须还是 `stage1.group`，
    否则既有磁带（都是一批的小模组）全部失配。那条断言在
    `test_one_batch_is_indistinguishable_from_before` 里。

    这里只钉住键的生成规则本身没被换掉。
    """
    client, _ = _run(180, batch_size=60)
    assert len(set(client.labels)) == len(client.labels)
    # 序号机制在起作用：同一个 label 第二次出现会带 `#2`
    assert any("#" in k or "batch" in k for k in client.labels[1:]), (
        "几批既没有批号也没有序号 ⇒ 磁带会互相覆盖"
    )


def test_the_same_entity_across_batches_keeps_all_its_items() -> None:
    """🔴 同一个实体被两批各归了一部分片段时，`item_ids` 要合并。

    这是我实现分批时**自己引入的一个 bug**：第一版遇到重复 id 直接 `continue`，
    于是后面几批分给它的片段在 `entities[].item_ids` 里凭空消失——而那个字段有
    下游消费方（stage2 渲染「包含片段」时读它）。

    **变异检验**：把合并那段换回 `continue`，这条当场红。
    """
    _, result = _run(120, entity_id_for=lambda iid: "loc-1", batch_size=60)

    entities = [e for e in result["entities"] if e["id"] == "loc-1"]
    assert len(entities) == 1, "同一个 id 不该出现两个实体"
    assert len(entities[0]["item_ids"]) == 120, "跨批的片段被丢了"


def test_a_batch_cannot_even_see_the_ids_outside_it() -> None:
    """🔴 **约束靠拿不到，不是请你别做**（2026-08-20 真机打脸后改的）。

    第一版照抄 `relation_probe` 的「焦点批 + 全局简表」，并在 user prompt 里写
    「本批之外的片段也在这里……**不要**为它们产出 assignment」。真机结果：
    batch0 只有 60 个片段，模型输出了 **24,500 字符 ≈ 241 条**——它把全部片段
    都做了，跟分批前一样截断。

    根因是 `STAGE1_SYSTEM` 写着「**全部**片段清单 / 把**每个片段**分配到一个
    归宿」——**两句话打架，system 赢**（判据：加了门要回头改被绕过的那句话，
    这是第三次）。

    所以现在批外的 id **根本不进上下文**。这条守的就是那件事。

    **变异检验**：把批外简表加回 user prompt，这条当场红。
    """
    client, _ = _run(180, batch_size=60)

    # batch0 拿到的是 it-0..it-59，绝不能出现 it-60 之后的任何 id
    first = client.prompts[0]
    for i in range(60, 180):
        assert f"it-{i}" not in first, f"批外的 it-{i} 泄进了 batch0 的上下文"


def test_later_batches_are_told_what_was_already_built() -> None:
    """🔴 跨批一致性**只**靠这一样：已建实体。

    没有它，第二批会给同一个场景另起一个 id（"地下室" / "cellar"），合并时谁
    也认不出它们是一回事。归组不需要跨批引用**片段** id，只需要跨批引用
    **实体** id——这正是它跟关系发现的差别，那边要找跨批的关系对。

    **变异检验**：把 `_format_known_entities` 改成恒返回 `""`，这条当场红。
    """
    client, _ = _run(180, batch_size=60)

    # 第一批之前什么都没建
    assert "前面几批已经建好的实体" not in client.prompts[0]
    for prompt in client.prompts[1:]:
        assert "前面几批已经建好的实体" in prompt
        assert "loc-1" in prompt


def test_the_system_prompt_does_not_contradict_batching() -> None:
    """🔴 **system 与 user 打架时 system 赢**——这条守住 system 那半也是分批语义。

    真机上正是这句话让整个分批失效：user 说"只做本批"，system 说"把每个片段
    分配到一个归宿"，模型听了 system 的。

    断言选得连反例都装不下：光断言 `"一批" in ...` 的话，「全部片段清单」原样
    留着也能通过。
    """
    from scripts.module_probe.assemble import STAGE1_SYSTEM

    assert "全部片段清单" not in STAGE1_SYSTEM, "这句是全量语义，会盖过 user 的分批指令"
    assert "把**每个片段**分配到一个归宿" not in STAGE1_SYSTEM
    assert "交给你的每个片段" in STAGE1_SYSTEM


@pytest.mark.parametrize("n", [1, 59, 60, 61, 120])
def test_boundaries_around_the_batch_size(n: int) -> None:
    """批大小边界：不多不少，正好每个片段一条。"""
    _, result = _run(n, batch_size=60)
    assert len(result["assignment_map"]) == n


# ── 两份映射自洽（2026-08-20，真机 trace 失败）──────────────


def _normalize(entities, assignments, valid_ids):
    from scripts.module_probe.assemble import _normalize_stage1

    return _normalize_stage1(
        {"entities": entities, "assignments": assignments, "orphans": []}, valid_ids
    )


def test_an_entity_claiming_an_item_gets_it_in_the_assignment_map() -> None:
    """🔴 真机：`node:third-day-trap-su ← 1 items` 明明认领了一个片段，
    `assignment_map` 里却没有任何片段指向它 ⇒ trace 判它「没有溯源锚点」，
    而三轮自修都修不掉——`repair#N.entity` 重吐的是实体内容，动不了映射。

    `entities[].item_ids` 与 `assignments` 是**同一个映射的两个方向**，模型的
    输出经常在两处对不上。合并这两份数据的人（就是这个函数）该负责让它们自洽。

    **变异检验**：删掉反向补齐那一段，这条当场红。
    """
    result = _normalize(
        entities=[{"kind": "node", "id": "trap", "title": "陷阱", "item_ids": ["it-1"]}],
        assignments=[],  # 模型忘了给 it-1 写 assignment
        valid_ids={"it-1"},
    )

    info = result["assignment_map"]["it-1"]
    assert info["dest_kind"] == "node"
    assert info["dest_id"] == "trap"
    # 补齐之后它就不是孤儿了
    assert not any(o["item_id"] == "it-1" for o in result["orphans"])


def test_it_does_not_steal_an_item_that_already_has_an_owner() -> None:
    """🔴 **只补没主的，不抢**。

    一个片段被两个实体同时认领，那是真正的归组冲突，该让校验报出来，不该在
    这里悄悄改掉——悄悄改会把「两个归宿」变成「最后一个赢」，而那不是修好了。

    **变异检验**：把条件里的 `or info["dest_kind"] == "unassigned"` 扩成无条件
    覆盖，这条当场红。
    """
    result = _normalize(
        entities=[{"kind": "node", "id": "trap", "title": "陷阱", "item_ids": ["it-1"]}],
        assignments=[{"item_id": "it-1", "dest_kind": "npc", "dest_id": "someone"}],
        valid_ids={"it-1"},
    )

    info = result["assignment_map"]["it-1"]
    assert info["dest_kind"] == "npc", "已经有主的片段被抢走了"
    assert info["dest_id"] == "someone"


def test_an_unassigned_item_is_claimable() -> None:
    """`unassigned` 等于没主，可以被认领——那正是补洞逻辑刚标上去的状态。"""
    result = _normalize(
        entities=[{"kind": "node", "id": "trap", "title": "陷阱", "item_ids": ["it-1"]}],
        assignments=[{"item_id": "it-1", "dest_kind": "unassigned", "dest_id": ""}],
        valid_ids={"it-1"},
    )

    assert result["assignment_map"]["it-1"]["dest_id"] == "trap"


# ── 输出撑爆时自动对半拆（2026-08-20，秘鲁序章）────────────


class _TruncatingCompletions:
    """替身：指定 label 前缀的调用返回**被截断的 JSON**，其余正常。"""

    def __init__(self, truncate_labels: tuple[str, ...]) -> None:
        # 🔴 **精确匹配，不是前缀**：第一版用 startswith，于是拆出来的
        # `batch0.a` 也被判成要截断，一路拆到单个片段——装置自己造了个假。
        self._truncate = frozenset(truncate_labels)
        self.labels: list[str] = []

    def create(self, **kwargs):
        label = kwargs.get("tape_key") or ""
        self.labels.append(label)
        user = kwargs["messages"][1]["content"]
        ids = [
            line.split("id: ", 1)[1].strip()
            for line in user.splitlines()
            if line.startswith("- id: ")
        ]

        class _Msg:
            content = ""

        if label in self._truncate:
            # 半截 JSON —— json.loads 会抛，_chat_json 重试三次后抛 RuntimeError
            _Msg.content = '{"entities": [{"kind": "node", "id": "x", "item_i'
        else:
            # 🔴 **看得见已建实体就复用它的 id**——模拟真实模型的行为，也是
            # `_absorb` 唯一起作用的场景。第一版装置每批都造新 id，于是
            # "右半看不见左半"这个变异体大摇大摆活了下来（造的样本没走到被测分支）。
            eid = f"loc-{ids[0]}"
            if "前面几批已经建好的实体" in user:
                known = [
                    line.split("/", 1)[1].split(":", 1)[0].strip()
                    for line in user.splitlines()
                    if line.startswith("- node/")
                ]
                if known:
                    eid = known[0]
            _Msg.content = json.dumps(
                {
                    "entities": [{"kind": "node", "id": eid, "title": "场景", "item_ids": ids}],
                    "assignments": [
                        {"item_id": i, "dest_kind": "node", "dest_id": eid} for i in ids
                    ],
                    "orphans": [],
                },
                ensure_ascii=False,
            )

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]
            usage = None

        return _Resp()


class _TruncatingClient:
    def __init__(self, labels: tuple[str, ...]) -> None:
        self.chat = type("_C", (), {"completions": _TruncatingCompletions(labels)})()

    @property
    def labels(self) -> list[str]:
        return self.chat.completions.labels


@pytest.fixture
def _no_backoff(monkeypatch):
    """`_chat_json` 每次重试前 sleep 1.5×attempt，测试里没必要真等。"""
    from scripts.module_probe import assemble

    monkeypatch.setattr(assemble.time, "sleep", lambda _s: None)


def test_a_batch_that_blows_the_output_limit_is_split_in_half(_no_backoff) -> None:
    """🔴 **秘鲁序章**：161 片段（比坨子岛的 218 还少）却在 batch0 就截断。

        坨子岛：45,011 字符 / 218 片段 = 206 字符/片段
        秘鲁：  58,269 字符 / 161 片段 = 362 字符/片段（1.76 倍）

    片段的"大小"在模组之间差着一倍多——**按个数切批就是拿错了度量**。而按字符
    数切也只是换一个猜得准一点的阈值，下一份模组照样能找到反例。

    对半拆不需要任何阈值：撞上了就拆细，拆到跑通为止。

    **变异检验**：把 `except RuntimeError` 那段删掉，这条当场红。
    """
    client = _TruncatingClient(("stage1.group:batch0",))
    # batch0 整批会截断，它的两半（.a / .b）不会
    result = stage1_group(
        client,  # ty: ignore[invalid-argument-type]
        _items(120),
        [],
        CallStats(),
        batch_size=60,
    )

    assert len(result["assignment_map"]) == 120, "拆分之后有片段丢了"
    patched = [k for k, v in result["assignment_map"].items() if v["dest_kind"] == "unassigned"]
    assert not patched, f"这些片段是被补洞逻辑救回来的：{patched[:5]}"


def test_the_split_halves_get_their_own_tape_keys(_no_backoff) -> None:
    """拆出来的两半要有各自的磁带键，否则回放时互相覆盖。"""
    client = _TruncatingClient(("stage1.group:batch0",))
    stage1_group(
        client,  # ty: ignore[invalid-argument-type]
        _items(120),
        [],
        CallStats(),
        batch_size=60,
    )

    succeeded = [k for k in client.labels if not k.startswith("stage1.group:batch0#")]
    assert any(k.startswith("stage1.group:batch0.a") for k in succeeded)
    assert any(k.startswith("stage1.group:batch0.b") for k in succeeded)


def test_a_single_item_that_still_fails_is_not_swallowed(_no_backoff) -> None:
    """🔴 拆到单个片段还吐不完整，那不是长度问题——**如实往上抛**，不要假装成功。

    静默吞掉会让一份缺了内容的模组悄悄进库，那比失败更糟。

    **变异检验**：把 `if len(focus) <= 1: raise` 改成 `return 空结果`，这条当场红。
    """
    # 4 个片段一批，拆分树是：batch 整批 → .a/.b → .a.a/.a.b/... 全部列出
    client = _TruncatingClient(
        (
            "stage1.group",
            "stage1.group.a",
            "stage1.group.b",
            "stage1.group.a.a",
            "stage1.group.a.b",
            "stage1.group.b.a",
            "stage1.group.b.b",
        )
    )

    with pytest.raises(RuntimeError):
        stage1_group(
            client,  # ty: ignore[invalid-argument-type]
            _items(4),
            [],
            CallStats(),
            batch_size=4,
        )


def test_the_right_half_sees_what_the_left_half_just_built(_no_backoff) -> None:
    """🔴 拆分时，右半必须看得见左半刚建的实体。

    否则同一个场景会被两半各建一个 id（"地下室" / "cellar"），合并时谁也认不出
    它们是一回事——分批那条判据在**拆分**时同样成立，而拆分是另一条代码路径。

    **变异检验**：把 `_absorb(left)` 那行去掉，这条当场红。
    """
    client = _TruncatingClient(("stage1.group:batch0",))
    result = stage1_group(
        client,  # ty: ignore[invalid-argument-type]
        _items(120),
        [],
        CallStats(),
        batch_size=60,
    )

    # batch0 被拆成两半，两半应该归到同一个实体上
    dest_ids = {
        v["dest_id"] for k, v in result["assignment_map"].items() if int(k.split("-")[1]) < 60
    }
    assert len(dest_ids) == 1, f"batch0 的两半各建了一个实体：{dest_ids}"
