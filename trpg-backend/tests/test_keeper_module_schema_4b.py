"""4b 薄迁移：exits/contains/sub_nodes/forms/visibility_pairs 加载与渲染。"""

from pathlib import Path

from app.core.keeper.module_loader import (
    ScenarioModule,
    load_module,
    render_full,
    render_node,
    render_npc,
    render_visibility_pairs,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "keeper_module.json"


def test_fixture_loads_4b_fields() -> None:
    module = load_module(_FIXTURE)
    assert module.visibility_pairs
    hall = module.node_by_id("hall")
    assert hall is not None
    assert hall.exits == ["cellar"]
    assert "hall-footprints" in hall.contains
    assert module.node_by_id("hall-footprints") is not None
    assert module.node_by_id("hidden-safe") is not None  # 兼容 sub_node
    butler = module.npc_by_id("butler-secret")
    assert butler is not None
    assert butler.forms
    assert "butler-public" in butler.same_as


def test_render_full_includes_edges_and_pairs() -> None:
    text = render_full(load_module(_FIXTURE))
    assert "邻接（空间）" in text
    assert "包含：" in text
    assert "【密级配对（绝密）】" in text
    assert "butler-public" in text
    assert "butler-secret" in text
    assert "形态[" in text


def test_render_node_and_npc_surfaces() -> None:
    module = load_module(_FIXTURE)
    hall = module.node_by_id("hall")
    assert hall is not None
    node_text = render_node(hall)
    assert "hall-footprints" in node_text
    assert "公开摘要" in node_text

    npc = module.npc_by_id("butler-secret")
    assert npc is not None
    npc_text = render_npc(npc)
    assert "butler-cornered" in npc_text
    assert "同一实体链接" in npc_text

    pairs = render_visibility_pairs(module)
    assert "pair-butler-faces" in pairs


def test_legacy_module_without_4b_fields_loads() -> None:
    """旧 JSON 无新字段时仍能 validate。"""
    raw = {
        "meta": {"id": "legacy", "title": "旧模组"},
        "kp_truth": {"summary": "s", "key_facts": []},
        "player_intro": "intro",
        "nodes": [
            {
                "id": "a",
                "title": "A",
                "kp_text": "text",
                "leads_to": ["b"],
            },
            {"id": "b", "title": "B", "kp_text": "text2"},
        ],
        "npcs": [{"id": "n1", "name": "N"}],
        "endings": [],
        "agenda": [],
        "kp_guidance": {},
    }
    mod = ScenarioModule.model_validate(raw)
    assert mod.visibility_pairs == []
    assert mod.nodes[0].exits == []
    assert mod.nodes[0].sub_nodes == []
    assert mod.npcs[0].forms == []
    # 无 pairs 时整块省略
    assert "密级配对" not in render_full(mod)
