"""模组标注的理智检定点：局面块 + 已触发记账（`exec/31 #73`）。

真人实测实据：导入的模组 23 个节点里只有 1 处 `kind="san"` 检定点，
玩家 04:09:35 **进了那个节点**（`keeper.node` 事件），当轮裁决只发了一次
逃跑的敏捷对抗——SAN 一次没起；全局唯一那次理智检定反而掷在没标注的节点上。

这里验证的是**提醒这一半**（触发条件由代码判）。掷不掷仍由模型决定，是
`exec/20` 里登记的概率性改进，不要在这里写"必掷"的断言——那会把一条
概率性改进伪装成保证。
"""

from __future__ import annotations

import random
import re
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys, situation_blocks
from app.core.keeper.capabilities.san_check.schema import SanCheckRequest
from app.core.keeper.capabilities.san_check.state import (
    SAN_POINTS_FIRED_KEY,
    format_san_points,
    load_fired_san_points,
    occupied_node_ids,
    san_point_ref,
)
from app.core.keeper.capabilities.world_state.schema import StateUpdate
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.location_state import PLAYER_LOCATION_KEY
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY, SCENE_NAME_KEY
from app.core.keeper.runtime.turn_executor import execute_side_effects
from app.models.room import Character, Player, Room

_MODULE = ScenarioModule.model_validate(
    {
        "meta": {"id": "san-points-fixture", "title": "试验模组"},
        "kp_truth": {"summary": "石堆后面蹲着的不是人。"},
        "player_intro": "你在林子里走。",
        "nodes": [
            {
                "id": "shrine",
                "title": "石堆神龛",
                "kp_text": "石堆后面蹲着那个东西。",
                "checks": [
                    {"skill": "侦察", "skill_ids": ["spot-hidden"], "kind": "skill"},
                    {
                        "skill": "理智",
                        "kind": "san",
                        "difficulty": "普通",
                        "on_success": "0",
                        "on_failure": "1D6",
                    },
                ],
            },
            {"id": "road", "title": "土路", "kp_text": "什么都没有。"},
        ],
    }
)


def _state(node: str | None = None, per_player: dict[str, str] | None = None) -> dict:
    out: dict = {}
    if node is not None:
        out[CURRENT_NODE_KEY] = node
    if per_player:
        out[PLAYER_LOCATION_KEY] = ", ".join(f"{p}@{n}" for p, n in per_player.items())
    return out


# ── 局面块 ──────────────────────────────────────────


def test_block_is_empty_where_the_module_annotated_nothing() -> None:
    """退化保证：没标注理智检定点的节点上整块不渲染。"""
    assert format_san_points(_MODULE, _state(node="road"), (("p1", "阿福"),)) == ""


def test_block_lists_the_annotated_point_at_the_players_own_node() -> None:
    text = format_san_points(_MODULE, _state(node="shrine"), (("p1", "阿福"),))
    assert "石堆神龛" in text and "1D6" in text
    # 提醒必须说清"看见了才掷"——否则它会变成"进门就掷"，那是规则错的
    assert "目睹" in text and "还没看见" in text


def test_a_fired_point_stops_being_advertised() -> None:
    """🔴 没有这笔记账，玩家在这个节点待几轮就被提醒几轮 → 重复扣 SAN。"""
    fired = _state(node="shrine") | {SAN_POINTS_FIRED_KEY: san_point_ref("shrine", 1)}
    assert format_san_points(_MODULE, fired, (("p1", "阿福"),)) == ""


def test_split_party_sees_only_their_own_nodes() -> None:
    """分头：一个人在神龛、一个人在土路——只提醒神龛那一处，且只提醒一次。"""
    split = _state(node="road", per_player={"p1": "shrine"})
    text = format_san_points(_MODULE, split, (("p1", "阿福"), ("p2", "阿贵")))
    assert text.count("石堆神龛") == 1


def test_no_location_means_no_reminder() -> None:
    """人在剧本节点之外（`exec/31 #72` 清空之后）→ 没有"模组标注"可言。"""
    assert occupied_node_ids(_MODULE, {}, (("p1", "阿福"),)) == []
    assert format_san_points(_MODULE, {}, (("p1", "阿福"),)) == ""


def test_registered_as_a_situation_block_and_a_reserved_key() -> None:
    blocks = situation_blocks(_MODULE, _state(node="shrine"))
    assert any("理智检定点" in body for _order, body in blocks)
    # 记账键是代码写的，state_updates 碰不到
    assert SAN_POINTS_FIRED_KEY in reserved_state_keys()


def test_the_narrator_never_sees_the_loss_dice() -> None:
    """🔴 `exec/23 #77`：叙事器读到这块，就把 `0/1D6` 念给了玩家听。

    这块整段都是**写给裁决器的指令**（"必须在 `san_checks` 里发起、损失表达式
    照抄下面的数值"），而局面块两阶段共用。**保密靠拿不到**——正解是不喂给
    叙事器，不是在叙事 prompt 里再加一条"别念机制"（那是 v1 已被推翻的做法）。
    """
    state = _state(node="shrine")
    keeper = situation_blocks(_MODULE, state)
    narrator = situation_blocks(_MODULE, state, keeper_view=False)

    assert any("理智检定点" in body for _order, body in keeper), "裁决器仍然必须看得见"
    assert not any("理智检定点" in body for _order, body in narrator)
    # 对照：光断言标题没了不够——损失骰本身一个字都不能出现在叙事器那份里
    narrator_text = "".join(body for _order, body in narrator)
    assert not re.search(r"\d*[dD]\d+", narrator_text), narrator_text


# ── 记账（走真实执行链）────────────────────────────

_db_path = Path(tempfile.mkdtemp(prefix="trpg-san-points-test-")) / "san.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def solo() -> KeeperDeps:
    async with _session_factory() as db:
        room = Room(room_code="SAN001", room_name="理智房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="阿福", is_host=True)
        db.add(player)
        await db.flush()
        db.add(
            Character(
                room_id=room.id,
                player_id=player.id,
                status="complete",
                name="侦探福",
                occupation="私家侦探",
                age=32,
                gender="男",
                attributes={
                    "STR": 60,
                    "CON": 50,
                    "SIZ": 50,
                    "DEX": 70,
                    "APP": 50,
                    "INT": 80,
                    "POW": 50,
                    "EDU": 70,
                    "LUCK": 55,
                },
                derived_stats={"HP": 10, "MP": 10, "SAN": 70, "MOV": 8},
                skills={"spot-hidden": 70},
            )
        )
        await db.commit()
        room_id, player_id = room.id, player.id

    return KeeperDeps(
        room_id=room_id,
        player_id=player_id,
        session_factory=_session_factory,
        module=_MODULE,
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
        turn_player_ids=(player_id,),
        rng=random.Random(7),
    )


async def _keeper_state(deps: KeeperDeps) -> dict:
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        return dict(room.keeper_state or {})


def _arrive_at(node_id: str, scene_name: str) -> KeeperDecision:
    """一次到达：节点指针 + 场景名一起写（规则 4 要求的就是这两件事一起做）。"""
    return KeeperDecision(
        current_node_id=node_id,
        state_updates=[StateUpdate(subject="world", key=SCENE_NAME_KEY, value=scene_name)],
    )


async def test_issuing_a_san_check_marks_the_point_at_that_node(solo) -> None:
    deps = solo
    # 🔴 挪场景指针要**同时**声明新场景（2026-08-14 加的门）：规则原文要求的就是
    # 这两件事一起做，只写 node 是没有依据的改写——实测里指针被写回「调查起点」
    # 正是那个形状。这里照真实路径给两样。
    await execute_side_effects(deps, _arrive_at("shrine", "神龛"))
    decision = KeeperDecision(
        san_checks=[SanCheckRequest(loss_on_success="0", loss_on_failure="1D6", reason="目睹")]
    )
    _report, issues = await execute_side_effects(deps, decision)
    assert issues == []
    assert load_fired_san_points(await _keeper_state(deps)) == [san_point_ref("shrine", 1)]


async def test_a_san_check_somewhere_unannotated_marks_nothing(solo) -> None:
    """在没标注的节点上掷 SAN 完全合法（COC7 的基线表），但没有检定点可记。"""
    deps = solo
    await execute_side_effects(deps, _arrive_at("road", "路上"))
    decision = KeeperDecision(
        san_checks=[SanCheckRequest(loss_on_success="0", loss_on_failure="1D3", reason="尸体")]
    )
    await execute_side_effects(deps, decision)
    assert load_fired_san_points(await _keeper_state(deps)) == []
