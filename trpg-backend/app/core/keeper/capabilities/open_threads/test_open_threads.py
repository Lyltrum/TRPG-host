"""悬而未决的事：即兴出来的处境要有落点，而且必须能被显式关掉。

实据是真人实测（`exec/31`）那一句 `「拉开距离，但米-戈仍在追击」`——它只活在
那一段散文里，下一轮就没了。

两条判据各有断言：
- **地基 = 有 id 的东西**，id 由**代码**分配（同即兴地点 `loc-N`）。
- **`#46`：没有显式的结束就永远不结束**，所以关闭走 id 白名单字段。
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
from app.core.keeper.capabilities.open_threads.schema import NewThread
from app.core.keeper.capabilities.open_threads.state import (
    OPEN_THREADS_KEY,
    OPEN_THREADS_SEQ_KEY,
    format_open_threads,
    load_open_threads,
    load_thread_seq,
    next_thread_id,
)
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.contract.registry import Capability
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY
from app.core.keeper.runtime.turn_executor import execute_side_effects
from app.models.room import Player, Room

_MODULE = ScenarioModule.model_validate(
    {
        "meta": {"id": "threads-fixture", "title": "试验模组"},
        "kp_truth": {"summary": "林子里有东西。"},
        "player_intro": "你在林子里。",
        "nodes": [{"id": "woods", "title": "林子", "kp_text": "树很密。"}],
    }
)


# ── 存储与 id ────────────────────────────────────


def test_ids_are_allocated_by_code_and_never_reused() -> None:
    """🔴 让模型自己起 id 就是「不要用自由文本当标识符」的复发。

    只增不复用：复用会让复盘里两件事共用一个 id。
    """
    assert next_thread_id(0) == ("thread-1", 1)
    assert next_thread_id(1) == ("thread-2", 2)


def test_the_counter_falls_back_to_the_table_for_old_rooms() -> None:
    """这一片上线之前建的房间没有这个键。回落到表里最大号——不会倒退。"""
    assert load_thread_seq(None) == 0
    assert load_thread_seq({OPEN_THREADS_KEY: {"thread-3": {"text": "丙"}}}) == 3
    # 记过的以记的为准（哪怕表已经被关空了）
    assert load_thread_seq({OPEN_THREADS_KEY: {}, OPEN_THREADS_SEQ_KEY: 7}) == 7


def test_malformed_rows_are_dropped_whole() -> None:
    """半条记录比没有更糟：局面块会渲染出一个没有内容的 id。"""
    assert load_open_threads({OPEN_THREADS_KEY: {"thread-1": {"text": "  "}}}) == {}
    assert load_open_threads({OPEN_THREADS_KEY: {"thread-1": "不是字典"}}) == {}
    assert load_open_threads({OPEN_THREADS_KEY: "不是字典"}) == {}


# ── 注册 ─────────────────────────────────────────


def test_the_key_is_reserved_and_both_fields_are_permissioned() -> None:
    assert OPEN_THREADS_KEY in reserved_state_keys()
    merged = field_capabilities()
    # 开与关共用一条：分开就会出现"能开不能关"的半截状态（`#46` 的形状）
    assert merged["new_threads"] is Capability.TRACK_THREADS
    assert merged["resolved_threads"] is Capability.TRACK_THREADS


def test_the_decision_carries_both_halves() -> None:
    fields = set(KeeperDecision.model_fields)
    assert {"new_threads", "resolved_threads"} <= fields


# ── 局面块 ───────────────────────────────────────


def test_the_block_is_empty_when_nothing_is_pending() -> None:
    assert not any("悬而未决" in body for _order, body in situation_blocks(_MODULE, {}))


def test_the_block_lists_every_thread_with_its_id() -> None:
    """🔴 必须全量列出：这块就是模型挑 id 的白名单，没列出来的对它等于不存在，
    它会把同一件事重新开一条。"""
    state = {
        OPEN_THREADS_KEY: {
            "thread-1": {"text": "米-戈仍在追击"},
            "thread-2": {"text": "油灯只剩十几分钟"},
        }
    }
    body = "".join(b for _o, b in situation_blocks(_MODULE, state) if "悬而未决" in b)
    assert "thread-1" in body and "米-戈仍在追击" in body
    assert "thread-2" in body and "油灯只剩十几分钟" in body
    # #46 的落点：局面块每轮都要提醒"不写就一直挂着"
    assert "resolved_threads" in body


# ── 走真实执行链 ─────────────────────────────────

_db_path = Path(tempfile.mkdtemp(prefix="trpg-threads-test-")) / "threads.db"
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
        room = Room(room_code="THR001", room_name="线头房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="阿福", is_host=True)
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
        turn_player_ids=(player_id,),
        rng=random.Random(3),
    )


async def _threads(deps: KeeperDeps) -> dict[str, dict]:
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        return load_open_threads(room.keeper_state)


async def test_an_improvised_threat_survives_the_turn_it_was_invented_in(deps) -> None:
    """这条就是 `exec/31` 那个缺口本身：说出来的东西下一轮还在不在。"""
    report, issues = await execute_side_effects(
        deps, KeeperDecision(new_threads=[NewThread(text="米-戈仍在追击")])
    )
    assert issues == []
    assert any("米-戈仍在追击" in line for line in report)
    assert await _threads(deps) == {"thread-1": {"text": "米-戈仍在追击"}}

    # 下一轮它照样在局面块里（"活过一轮"是这条线的验收）
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        state = dict(room.keeper_state or {})
    body = "".join(b for _o, b in situation_blocks(_MODULE, state) if "悬而未决" in b)
    assert "米-戈仍在追击" in body


async def test_resolving_it_takes_it_off_the_board(deps) -> None:
    """🔴 `#46` 的正面用例：写了 id 才关掉。"""
    await execute_side_effects(deps, KeeperDecision(new_threads=[NewThread(text="米-戈仍在追击")]))
    report, issues = await execute_side_effects(deps, KeeperDecision(resolved_threads=["thread-1"]))
    assert issues == []
    assert any("已了结" in line for line in report)
    assert await _threads(deps) == {}


async def test_a_made_up_id_is_refused_not_silently_ignored(deps) -> None:
    """编造的 id 不该悄悄变成"关掉了什么都没发生"。"""
    report, issues = await execute_side_effects(deps, KeeperDecision(resolved_threads=["thread-9"]))
    assert report == []
    assert issues and "thread-9" in issues[0]


async def test_closing_and_opening_in_the_same_turn_does_not_collide(deps) -> None:
    """🔴 先关后开：同一轮"这件事了结了、但引出了新的一件"是常见形状。
    先开后关的话，新开的那条会拿到刚被腾出来的 id、然后被同一轮的关闭删掉。"""
    await execute_side_effects(deps, KeeperDecision(new_threads=[NewThread(text="米-戈仍在追击")]))
    await execute_side_effects(
        deps,
        KeeperDecision(
            resolved_threads=["thread-1"],
            new_threads=[NewThread(text="它带回来了同伴")],
        ),
    )
    remaining = await _threads(deps)
    assert list(remaining.values()) == [{"text": "它带回来了同伴"}]
    # 新的那条**不能**叫 thread-1（那个 id 已经用过了）
    assert "thread-1" not in remaining


async def test_an_empty_text_is_an_issue_not_a_blank_row(deps) -> None:
    report, issues = await execute_side_effects(
        deps, KeeperDecision(new_threads=[NewThread(text="   ")])
    )
    assert report == []
    assert issues
    assert await _threads(deps) == {}


async def test_the_model_cannot_overwrite_the_table_through_state_updates(deps) -> None:
    """保留键：一条 `state_updates` 把这张表覆盖成字符串的话，记账静默清零。"""
    from app.core.keeper.capabilities.world_state.schema import StateUpdate

    await execute_side_effects(deps, KeeperDecision(new_threads=[NewThread(text="米-戈仍在追击")]))
    _report, issues = await execute_side_effects(
        deps,
        KeeperDecision(
            state_updates=[StateUpdate(subject="world", key=OPEN_THREADS_KEY, value="没了")]
        ),
    )
    assert issues and "系统记账" in issues[0]
    assert (await _threads(deps))["thread-1"]["text"] == "米-戈仍在追击"


# ── 作用域：一条处境在哪成立（2026-08-14 实测） ─────────────────────


def test_a_thread_opened_elsewhere_is_listed_apart_from_the_ones_here() -> None:
    """🔴 实测：`「看护仍在身后追赶」`在疗养院开出来，跟着玩家跨了三个地点、
    追了 25 分钟，最后跟米-戈同框——因为它只有 `{id, text}`，没有作用域。

    修法不是自动关掉（追击**可以**跨地点，自动关会误杀），而是把判断的输入
    摆准：离开原地之后单列一组，明说"多半已经不成立"。
    """
    state = {
        OPEN_THREADS_KEY: {
            "thread-1": {"text": "看护仍在身后追赶", "node": "asylum"},
            "thread-2": {"text": "地窖里有东西在动", "node": "cellar"},
        },
        CURRENT_NODE_KEY: "cellar",
    }
    text = format_open_threads(state)
    here, elsewhere = text.split("🔴")
    assert "地窖里有东西在动" in here
    assert "每一轮都仍然成立" in here
    assert "看护仍在身后追赶" in elsewhere
    assert "已经不成立" in elsewhere
    assert "asylum" in elsewhere  # 说清楚是在哪开的


def test_a_thread_left_behind_is_still_listed_so_it_can_be_closed() -> None:
    """🔴 降级**不能**变成不列出：这块是模型挑 id 的白名单，没列出来的它就
    关不掉，那条记录会永远躺在 keeper_state 里（`#46` 那个形状）。"""
    state = {
        OPEN_THREADS_KEY: {"thread-1": {"text": "看护仍在身后追赶", "node": "asylum"}},
        CURRENT_NODE_KEY: "cellar",
    }
    text = format_open_threads(state)
    assert "thread-1" in text
    assert "resolved_threads" in text


def test_threads_without_a_node_behave_exactly_as_before() -> None:
    """老对局的条目没有 `node`——那表示"不知道在哪成立"，按到处都成立处理，
    与加这个字段之前**逐字一致**（退化保证）。"""
    state = {
        OPEN_THREADS_KEY: {"thread-1": {"text": "米-戈仍在追击"}},
        CURRENT_NODE_KEY: "cellar",
    }
    text = format_open_threads(state)
    assert "🔴" not in text
    assert "每一轮都仍然成立" in text


async def test_opening_a_thread_records_where_it_happened(deps) -> None:
    """写入侧：开的时候就记下当前节点，否则读出来永远没有作用域。"""
    async with deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        room.keeper_state = {**(room.keeper_state or {}), CURRENT_NODE_KEY: "asylum"}
        await db.commit()

    await execute_side_effects(deps, KeeperDecision(new_threads=[NewThread(text="看护在追")]))
    assert (await _threads(deps))["thread-1"]["node"] == "asylum"
