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


def test_every_batch_sees_the_whole_catalog_and_what_was_already_built() -> None:
    """🔴 分批与全量的语义差别全在这两样上，缺一样就会归错。

    - **全局简表**：没有它，模型看不到批外片段，实体边界会画错；
    - **已建实体**：没有它，第二批会给同一个场景另起一个 id
      （"地下室" / "cellar"），而合并时谁也认不出它们是一回事。

    **变异检验**：把 `_format_known_entities` 改成恒返回 `""`，这条当场红。
    """
    client, _ = _run(180, batch_size=60)

    for prompt in client.prompts:
        assert "全部 180 个片段的 id / title 简表" in prompt, "某一批没带全局简表"
    # 第一批之前什么都没建，之后每批都该看到已建实体
    assert "前面几批已经建好的实体" not in client.prompts[0]
    for prompt in client.prompts[1:]:
        assert "前面几批已经建好的实体" in prompt
        assert "loc-1" in prompt


@pytest.mark.parametrize("n", [1, 59, 60, 61, 120])
def test_boundaries_around_the_batch_size(n: int) -> None:
    """批大小边界：不多不少，正好每个片段一条。"""
    _, result = _run(n, batch_size=60)
    assert len(result["assignment_map"]) == n
