"""对抗检定的「对手」栏是给玩家看的——填了 NPC id 就翻成名字（2026-08-18 真机）。

实测那一局最后一次对抗写的是 `opposedOpponent = "mi-go-4"`，前几次写的是
「米-戈 #4」。这一栏在 schema 里就是自由文本（「NPC 名、毒物、暗流……，展示用」），
压不住；而 **id → 名字是完全确定的映射**，代码这边免费翻掉即可。
"""

from __future__ import annotations

from app.core.keeper.capabilities.skill_check.executor import _display_opponent
from app.core.keeper.contract.module_loader import ScenarioModule


def _module() -> ScenarioModule:
    return ScenarioModule.model_validate(
        {
            "meta": {"id": "m", "title": "测试模组"},
            "kp_truth": {"summary": "无关紧要"},
            "player_intro": "开场",
            "npcs": [
                {"id": "watcher-4", "name": "守望者 #4"},
                {
                    "id": "shape-shifter",
                    "name": "变形者",
                    "forms": [{"id": "shape-shifter-revealed", "name": "揭穿后的变形者"}],
                },
            ],
            "nodes": [{"id": "n1", "title": "起点", "kind": "location", "kp_text": "空"}],
        }
    )


def test_an_npc_id_becomes_its_display_name() -> None:
    assert _display_opponent(_module(), "watcher-4") == "守望者 #4"


def test_a_form_id_resolves_to_the_form_not_the_body() -> None:
    """认形态，跟 `resolve_npc_ref` 配套——上溯到本体会答错人。"""
    assert _display_opponent(_module(), "shape-shifter-revealed") == "揭穿后的变形者"


def test_a_name_that_is_already_a_name_survives() -> None:
    assert _display_opponent(_module(), "守望者 #4") == "守望者 #4"


def test_something_that_is_not_an_npc_is_left_alone() -> None:
    """🔴 对手可以根本不是 NPC —— 毒物、暗流、一扇卡住的门。那是正常展示文本。"""
    for text in ("毒物（POT 16）", "湍急的暗流", "卡死的舱门"):
        assert _display_opponent(_module(), text) == text
