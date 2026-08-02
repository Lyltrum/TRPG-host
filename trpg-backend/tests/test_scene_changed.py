"""场景切换检测（`location_state.scene_changed`）。

这个函数在 `exec/27` 阶段 4 之前埋在 `agent.narrate` 那条 480 行的主流程里，
**没有一条直接测试**——它的行为只能靠跑整轮叙事间接观察。抽出来之后才发现
它其实只依赖三样东西（前后两份 keeper_state + 本轮发言的人），是个纯函数。

> 一个纯函数埋在长流程里，代价不是"不好看"，是**它没有便宜的验证器**。
"""

from __future__ import annotations

from app.core.keeper.location_state import PLAYER_LOCATION_KEY, scene_changed
from app.core.keeper.scene_state import CURRENT_NODE_KEY, SCENE_NAME_KEY


def _state(
    *, node: str | None = None, per_player: dict[str, str] | None = None, scene: str | None = None
) -> dict:
    out: dict = {}
    if node is not None:
        out[CURRENT_NODE_KEY] = node
    if per_player:
        out[PLAYER_LOCATION_KEY] = ", ".join(f"{pid}@{nid}" for pid, nid in per_player.items())
    if scene is not None:
        out[SCENE_NAME_KEY] = scene
    return out


def test_moving_to_another_node_counts_as_a_change() -> None:
    assert scene_changed(_state(node="hall"), _state(node="cellar"), ["p1"]) is True


def test_staying_put_is_not_a_change() -> None:
    assert scene_changed(_state(node="hall"), _state(node="hall"), ["p1"]) is False


def test_per_player_location_wins_over_the_room_pointer() -> None:
    """🔴 P5.2：分头之后房间不再有单一"当前场景"，判据必须逐人问。

    房间指针没动，但这个人被 `moves` 挪走了——这一轮对他就是换了场景。
    """
    before = _state(node="hall")
    after = _state(node="hall", per_player={"p1": "cellar"})
    assert scene_changed(before, after, ["p1"]) is True


def test_only_the_speakers_are_examined() -> None:
    """没发言的人挪没挪，不构成"本轮换了场景"。"""
    before = _state(node="hall")
    after = _state(node="hall", per_player={"p2": "cellar"})
    assert scene_changed(before, after, ["p1"]) is False


def test_falls_back_to_the_human_readable_scene_name() -> None:
    """兼容尚未产出 node id 的模组与历史房间：两端都没有 node_id 时比地名。"""
    assert scene_changed(_state(scene="门厅"), _state(scene="地下室"), ["p1"]) is True
    assert scene_changed(_state(scene="门厅"), _state(scene="门厅"), ["p1"]) is False


def test_a_missing_scene_name_on_either_side_is_not_a_change() -> None:
    """🔴 "不知道"不等于"变了"。

    没有这一条，每一个还没记过场景的新房间第一轮都会被判成场景切换，白白多
    一段过渡叙事。
    """
    assert scene_changed({}, _state(scene="门厅"), ["p1"]) is False
    assert scene_changed(_state(scene="门厅"), {}, ["p1"]) is False
    assert scene_changed(None, None, ["p1"]) is False


def test_nobody_spoke_means_nothing_moved() -> None:
    assert scene_changed(_state(node="hall"), _state(node="cellar"), []) is False
