"""台上此刻有谁：叙事里的 NPC 必须能对上模组名册。

实据是真人实测（2026-08-14）：路边小屋里那个「瘦小的酗酒老头」其实就是名册上
的卡比·卡普顿，模型没把两者联系起来——玩家问「你知道这个卡比是什么吗」，
主持人让**老头指路去找他自己**。

判据：**位置有 id、线索有 id、悬而未决有 id，唯独台上的人没有。**
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import field_capabilities, reserved_state_keys, situation_blocks
from app.core.keeper.capabilities.cast.state import (
    ON_STAGE_KEY,
    format_on_stage,
    load_on_stage,
)
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.contract.registry import Capability
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.turn_executor import execute_side_effects
from app.models.room import Player, Room

_MODULE = ScenarioModule.model_validate(
    {
        "meta": {"id": "cast-fixture", "title": "试验模组"},
        "kp_truth": {"summary": "老头就是卡比。"},
        "player_intro": "你在路边停下。",
        "nodes": [{"id": "roadside", "title": "路边小屋", "kp_text": "屋里有人。"}],
        "npcs": [
            {"id": "cappy-capton", "name": "卡比·卡普顿", "role": "酗酒的当地居民"},
            {"id": "devereaux", "name": "艾伦·德弗罗", "role": "委托人"},
        ],
    }
)

_db_path = Path(tempfile.mkdtemp(prefix="trpg-cast-")) / "cast.db"
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
async def deps() -> KeeperDeps:
    async with _session_factory() as db:
        room = Room(room_code="CAST01", room_name="登场房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="凌铭辉")
        db.add(player)
        await db.commit()
        room_id, player_id = room.id, player.id

    return KeeperDeps(
        room_id=room_id,
        player_id=player_id,
        session_factory=_session_factory,
        module=_MODULE,
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
        rng=random.Random(3),
    )


async def _on_stage(deps: KeeperDeps) -> list[str]:
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        return load_on_stage(room.keeper_state)


# ── 存储与渲染 ──────────────────────────────────────


def test_the_block_names_them_and_forbids_vague_references() -> None:
    text = format_on_stage(_MODULE, {ON_STAGE_KEY: "cappy-capton"})
    assert "cappy-capton" in text and "卡比·卡普顿" in text
    # 🔴 光列出来不够：得说清「不要另起称呼」，那正是同一个人变两个的入口
    assert "不要另起称呼" in text
    # 也得说清「他们已经登场了」——实测那次的形状是让台上的人去打听台上的人
    assert "去打听另一个" in text


def test_an_empty_stage_renders_nothing() -> None:
    """退化保证：台上没人时整块不渲染。"""
    assert format_on_stage(_MODULE, {}) == ""
    assert format_on_stage(_MODULE, {ON_STAGE_KEY: ""}) == ""


def test_registered_as_a_block_a_field_and_a_reserved_key() -> None:
    # 台上有人才渲染（空的时候整块不出现，见上一条）
    blocks = situation_blocks(_MODULE, {ON_STAGE_KEY: "cappy-capton"})
    assert any("此刻在场的 NPC" in body for _order, body in blocks)
    assert field_capabilities()["npcs_on_stage"] is Capability.SET_SCENE
    assert ON_STAGE_KEY in reserved_state_keys()


# ── 执行 ────────────────────────────────────────────


async def test_writing_the_cast_resolves_names_to_ids(deps: KeeperDeps) -> None:
    """名字也认（走 `resolve_npc_id`，跟 hp_changes / state_updates 同一个解析器），
    但**存进去的一律是 id**。"""
    _report, issues = await execute_side_effects(
        deps, KeeperDecision(npcs_on_stage=["卡比·卡普顿"])
    )
    assert issues == []
    assert await _on_stage(deps) == ["cappy-capton"]


async def test_a_made_up_npc_is_refused_not_silently_added(deps: KeeperDeps) -> None:
    """白名单外的 id 一律拒绝——与 NPC 血量/状态主体/议程/密级的处理一致。
    编造的名字进了状态，就又回到「自由文本当标识符」。"""
    _report, issues = await execute_side_effects(
        deps, KeeperDecision(npcs_on_stage=["cappy-capton", "路边那个老头"])
    )
    assert any("路边那个老头" in i for i in issues)
    assert await _on_stage(deps) == ["cappy-capton"]


async def test_the_cast_is_a_snapshot_not_an_increment(deps: KeeperDeps) -> None:
    """🔴 快照语义：这一轮谁在就是谁，走掉的不该留在台上。

    增量语义会让台上的人只增不减——那正是「没有显式的结束就永远不结束」
    （`#46`）的形状。
    """
    await execute_side_effects(deps, KeeperDecision(npcs_on_stage=["cappy-capton", "devereaux"]))
    assert await _on_stage(deps) == ["cappy-capton", "devereaux"]

    await execute_side_effects(deps, KeeperDecision(npcs_on_stage=["cappy-capton"]))
    assert await _on_stage(deps) == ["cappy-capton"]

    # 空数组 = 场景里一个 NPC 都没有，也要落下去
    await execute_side_effects(deps, KeeperDecision(npcs_on_stage=[]))
    assert await _on_stage(deps) == []


async def test_the_cast_never_lands_in_the_execution_report(deps: KeeperDeps) -> None:
    """🔴 执行报告只装「世界变了什么」。「台上有谁」是这一轮的输入快照，
    不是发生的事——往报告里多塞一行会当场打红别的能力那些"本轮报告有几条"
    的断言（`exec/37` 踩过一次）。"""
    report, _issues = await execute_side_effects(
        deps, KeeperDecision(npcs_on_stage=["cappy-capton"])
    )
    assert not any("cappy-capton" in line for line in report)


async def test_the_block_carries_the_cast_into_the_next_turn(deps: KeeperDeps) -> None:
    """闭环：写进去的东西下一轮真的摆到模型眼前了。

    没有这一条，这一片就是「加了字段没有消费方」——两头都不会变红。
    """
    await execute_side_effects(deps, KeeperDecision(npcs_on_stage=["cappy-capton"]))
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        state = room.keeper_state
    blocks = situation_blocks(_MODULE, state)
    assert any("卡比·卡普顿" in body for _order, body in blocks)
