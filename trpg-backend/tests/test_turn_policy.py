"""本轮能力撤销（exec/27 阶段 3 · B 族）。

替掉的是 `agent.py` 里四处 `model_copy(update={"checks": [], ...})`。这份用例
守的是**替换前后行为逐条相同**，以及那次为了不改行为而做的能力拆分。
"""

from __future__ import annotations

from app.core.keeper.capabilities.health.schema import HpChange
from app.core.keeper.capabilities.movement.schema import HidingChange, PlayerMove
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.registry import Capability
from app.core.keeper.runtime.turn_policy import (
    CHECK_CAPABILITIES,
    SCENE_ADVANCE_CAPABILITIES,
    revoke,
)


def _full_decision() -> KeeperDecision:
    from app.core.keeper.capabilities.san_check.schema import SanCheckRequest
    from app.core.keeper.capabilities.skill_check.schema import CheckRequest

    return KeeperDecision(
        thinking="都填上",
        narration_guidance="写故事",
        checks=[CheckRequest(skill_id="spot-hidden")],
        san_checks=[SanCheckRequest()],
        hp_changes=[HpChange(delta=-2)],
        current_node_id="cellar",
        moves=[PlayerMove(player="阿铁", node_id="cellar")],
        hiding=[HidingChange(player="阿铁", hidden=True)],
    )


def test_revoking_nothing_returns_the_very_same_object() -> None:
    """绝大多数轮次一次拷贝都不该发生。"""
    decision = _full_decision()
    assert revoke(decision, frozenset()) is decision


def test_revoking_checks_clears_both_check_kinds_and_nothing_else() -> None:
    """心跳 / 迷茫 / 怪话轮：只收走发起检定的权力。"""
    result = revoke(_full_decision(), CHECK_CAPABILITIES)
    assert result.checks == [] and result.san_checks == []
    # 其余一概不动——旧代码那三处 model_copy 也只列了这两个字段
    assert result.current_node_id == "cellar"
    assert len(result.moves) == 1
    assert len(result.hp_changes) == 1


def test_asking_the_keeper_freezes_the_world_but_not_hiding() -> None:
    """🔴 `asks_kp` 那一轮收走检定 + 移动 + 场景指针，**保留隐匿**。

    藏没藏起来是已经成立的状态，不因为玩家问了句话就现身。这正是
    `SET_HIDING` 必须与 `SET_SCENE` 分开的理由——合成一条的话，这次
    「清字段 → 撤能力」的替换就会顺手把人从阴影里拽出来。
    """
    result = revoke(_full_decision(), CHECK_CAPABILITIES | SCENE_ADVANCE_CAPABILITIES)
    assert result.checks == [] and result.san_checks == []
    assert result.moves == []
    assert result.current_node_id is None
    # 隐匿活下来了
    assert len(result.hiding) == 1 and result.hiding[0].hidden is True


def test_hiding_and_scene_are_two_distinct_capabilities() -> None:
    assert Capability.SET_HIDING not in SCENE_ADVANCE_CAPABILITIES
    assert Capability.SET_HIDING is not Capability.SET_SCENE
