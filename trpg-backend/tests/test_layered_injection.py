"""剧本分层注入的两条路（`exec/47` P1b）。

## 🔴 这个文件存在的理由

P1b 落地之后跑全套，**2272 条一条都没红**——而那不是"改对了"的证据，是
**分层这条分支根本没被走到**：整套测试的 fixture 都是几千字符的迷你剧本，
全部落在退化路径上。「造的样本没走到被测分支 = 没测」。

所以这里刻意造一份**超过阈值**的模组。它是合成的，而合成样本在这个仓库里
栽过（`exec/41` 拿合成模组估出来的召回体量高了三到四倍）——**区别在于这里
测的是代码分支，不是判据**：阈值、召回量、孤立率那些数已经由八份真模组量过
（`test_module_layered_index.py`）。合成样本只负责"把两条路都走一遍"。

## 两条路各自要成立的事

- **短模组（退化）**：system prompt 与局面块都与分层之前**逐字节一致**。
  分层不是免费的——模型判断「从这儿能去哪」原本靠的就是整份剧本在 prompt 里。
- **长模组（分层）**：正文换成索引；孤立节点正文常驻；当前这几处的正文经由
  **局面块**送达，而不是 system prompt（那会废掉前缀缓存）。
"""

from __future__ import annotations

from app.core.keeper.contract.module_loader import (
    ScenarioModule,
    render_full,
    render_layered,
    render_recall,
)
from app.core.keeper.narration.prompts import (
    build_narrator_instructions,
    format_turn_input,
    render_script,
)
from app.core.keeper.narration.situation import SituationBuilder
from app.core.keeper.runtime.focus import (
    LAYERED_SCRIPT_THRESHOLD,
    ids_mentioned_by,
    isolated_node_ids,
    should_layer,
)
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY


#: 每个节点的正文里塞一句独一无二的话，用来断言"这段正文到底在不在 prompt 里"。
#: 断言选的是**整句**而不是某个词——反例装不下（判据：断言的子串要选得连反例
#: 都装不下）。
def _body(node_id: str) -> str:
    return f"【{node_id}】这里藏着只有 {node_id} 才有的那句话。" + "细节。" * 300


def _module(node_count: int, *, isolated: int = 0) -> ScenarioModule:
    nodes = []
    for i in range(node_count):
        node_id = f"node-{i}"
        # 串成一条链，最后 `isolated` 个不连任何边
        edges = {} if i >= node_count - isolated else {"leads_to": [f"node-{i + 1}"]}
        if i == node_count - isolated - 1:
            edges = {}
        nodes.append({"id": node_id, "title": f"第{i}处", "kp_text": _body(node_id), **edges})
    return ScenarioModule.model_validate(
        {
            "meta": {"id": "big", "title": "大模组"},
            "kp_truth": {"summary": "真相", "key_facts": []},
            "player_intro": "开场",
            "nodes": nodes,
            "npcs": [
                {"id": "npc-a", "name": "甲", "kp_notes": "甲的秘密独此一份。"},
                {"id": "npc-b", "name": "乙", "kp_notes": "乙的秘密独此一份。"},
            ],
        }
    )


_SHORT = _module(3)
_LONG = _module(30, isolated=2)


def test_the_fixtures_really_straddle_the_threshold() -> None:
    """🔴 装置自证。没有这条，下面每一条都可能在测同一条路。"""
    assert len(render_full(_SHORT)) < LAYERED_SCRIPT_THRESHOLD
    assert len(render_full(_LONG)) > LAYERED_SCRIPT_THRESHOLD
    assert not should_layer(_SHORT)
    assert should_layer(_LONG)


# ═══ 退化路径：逐字节一致 ═══


def test_a_short_module_is_injected_exactly_as_before() -> None:
    """短模组的 system prompt 必须与分层之前**逐字节相同**。"""
    assert render_script(_SHORT) == render_full(_SHORT)


def test_a_short_module_adds_nothing_to_the_situation_block() -> None:
    """局面块同理：召回段是空串 ⇒ 整块不渲染。"""
    before = format_turn_input(None, [], ["甲"], "甲", "你好")
    after = format_turn_input(None, [], ["甲"], "甲", "你好", script_recall="")
    assert before == after
    assert "本轮相关剧本" not in after


# ═══ 分层路径 ═══


def test_a_long_module_gets_an_index_instead_of_bodies() -> None:
    script = render_script(_LONG)
    assert script != render_full(_LONG)
    # 索引在：每个节点的 id 都要出现，否则模型连它存在都不知道
    for i in range(30):
        assert f"node-{i}" in script
    # 正文不在：随便挑一个连着别人的节点，它的正文该由召回给，不该常驻
    assert _body("node-5") not in script


def test_isolated_nodes_keep_their_bodies_resident() -> None:
    """🔴 它们进不了任何关注集 ⇒ 正文不常驻的话模型**永远**看不到。"""
    isolated = isolated_node_ids(_LONG)
    assert isolated, "装置自证：这份模组确实有孤立节点"
    script = render_script(_LONG)
    for node_id in isolated:
        assert _body(node_id) in script, f"{node_id} 孤立却没常驻——模型永远读不到它"


def test_both_system_prompts_see_the_same_form_of_the_script() -> None:
    """🔴 裁决分层、叙事却拿整份，是最坏的一种不一致。

    裁决按索引挑了一个节点，叙事那边却在整份剧本里看见了别的——**两边都不会
    报错**，表现只是"叙事写的跟裁决判的不是一回事"。`render_script` 的
    docstring 写着"两个 system prompt 必须走同一个函数"，这条就是它的守门人。

    （写这个文件时六条变异里正是这一条没红——判据写在注释里没有守门人，
    等于没写。）
    """
    narrator = build_narrator_instructions(_LONG)
    assert _body("node-5") not in narrator, "叙事那份还是整份剧本"
    for i in range(30):
        assert f"node-{i}" in narrator, "叙事那份连索引都没有"


def test_the_layered_script_is_much_smaller() -> None:
    assert len(render_script(_LONG)) < len(render_full(_LONG)) / 2


def test_the_prologue_and_endings_are_identical_in_both_forms() -> None:
    """前四段与结局在两种形态下逐字共用——它们只占 ~10%，分层没有收益。"""
    layered = render_layered(_LONG, frozenset())
    full = render_full(_LONG)
    for shared in ("【KP 真相（绝密）】真相", "═══ 结局 ═══"):
        assert shared in layered and shared in full


# ═══ 召回段经由局面块送达 ═══


def _situation(state: dict | None, decision: object | None = None) -> str:
    builder = SituationBuilder(
        room_id="r",
        visible_state=None,
        history_lines=[],
        roster=["甲"],
        phase=None,
        phase_status="",
        ledger_status="",
        chapters=[],
        capability_blocks=[],
        narrator_capability_blocks=[],
        is_heartbeat=False,
        is_opening_ceremony=False,
        module=_LONG,
        raw_state=state,
    )
    return builder.render(
        audience=None, ledger="", nickname="甲", utterance="嗯", decision=decision
    )


def test_the_body_of_the_node_you_stand_on_arrives_via_the_situation_block() -> None:
    text = _situation({CURRENT_NODE_KEY: "node-5"})
    assert "本轮相关剧本" in text
    assert _body("node-5") in text, "人就站在这儿，它的正文必须到场"
    assert _body("node-6") in text, "关联节点也该到场"
    assert _body("node-20") not in text, "八竿子打不着的节点不该被拉进来"


def test_a_node_only_the_decision_knows_about_still_arrives() -> None:
    """🔴 **P1b 最容易坏的一条。**

    局面块整轮只建一次，用的是**裁决之前**的 keeper_state；而叙事那一拍最需要
    的恰恰是玩家刚走到的新节点——它此刻只存在于裁决输出里。不带 decision 的话
    「我去地窖」这一拍的叙事会拿不到地窖写着什么，只能瞎编，**而且不会报错**。
    """

    class _Decision:
        def model_dump(self) -> dict:
            return {"current_node_id": "node-20", "thinking": "他要去第20处"}

    without = _situation({CURRENT_NODE_KEY: "node-5"})
    with_decision = _situation({CURRENT_NODE_KEY: "node-5"}, _Decision())
    assert _body("node-20") not in without, "装置自证：不给 decision 时它确实拿不到"
    assert _body("node-20") in with_decision


def test_ids_are_harvested_from_every_field_not_a_hand_written_list() -> None:
    """加一片新能力不该让它的 node id 悄悄漏掉（「逐个列出，加一项就漏一项」）。"""

    class _Decision:
        def model_dump(self) -> dict:
            return {"某个还没发明的能力": {"nested": ["node-20"]}}

    assert "node-20" in ids_mentioned_by(_Decision())
    assert _body("node-20") in _situation({CURRENT_NODE_KEY: "node-5"}, _Decision())


def test_recall_of_nothing_renders_nothing() -> None:
    assert render_recall(_LONG, frozenset(), frozenset()) == ""
