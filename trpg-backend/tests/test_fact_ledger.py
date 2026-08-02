"""线索账本 L1（exec/14 P4）。

核心断言是计划里那条：**合成一局 300+ 事件的对局，第 3 轮拿到的线索在
第 200 轮之后仍然可用**——即账本活过 `HISTORY_LIMIT` 的滑动窗口。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import Base
from app.core.keeper.contract.module_loader import (
    KeeperTruth,
    ModuleFact,
    ModuleMeta,
    ScenarioModule,
)
from app.core.keeper.memory.fact_ledger import (
    record_revelations,
    render_ledger,
    revealed_fact_ids,
    revelations,
)
from app.core.keeper.memory.history import HISTORY_LIMIT
from app.models.event import Event
from app.models.room import Player, Room

EARLY_CLUE = "门厅地毯上有半干的泥脚印"


def _module() -> ScenarioModule:
    return ScenarioModule(
        meta=ModuleMeta(id="m", title="合成"),
        kp_truth=KeeperTruth(summary="真相"),
        player_intro="开场",
        facts=[
            ModuleFact(id="fact-001", text=EARLY_CLUE),
            ModuleFact(id="fact-002", text="书房窗闩是从外面撬开的"),
            ModuleFact(id="fact-meta", text="真凶其实是管家", kind="truth", tier="meta"),
        ],
    )


@pytest.fixture
async def room(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/ledger.db", poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as db:
        r = Room(room_code="LEDGER", room_name="账本房", max_players=4, phase="InGame")
        db.add(r)
        await db.flush()
        p = Player(room_id=r.id, nickname="调查员甲", is_host=True)
        db.add(p)
        await db.commit()
        ids = (r.id, p.id)
    yield factory, ids
    await engine.dispose()


@pytest.mark.asyncio
async def test_revelation_is_recorded_with_provenance(room) -> None:
    """出处（谁/何时/经由什么）现在就记全——P5 的 per-observer 视图要用。"""
    factory, (room_id, player_id) = room
    async with factory() as db:
        await record_revelations(
            db,
            room_id=room_id,
            player_id=player_id,
            fact_ids=["fact-001"],
            via="check",
            detail="侦察·困难成功",
        )
        await db.commit()

    async with factory() as db:
        entries = await revelations(db, room_id=room_id)
    assert len(entries) == 1
    assert entries[0].fact_id == "fact-001"
    assert entries[0].player_id == player_id
    assert entries[0].via == "check"
    assert "困难成功" in entries[0].detail


@pytest.mark.asyncio
async def test_same_fact_via_two_paths_is_recorded_once(room) -> None:
    """多路径线索：同一条信息经不同检定拿到，账本只算一次（否则重复渲染）。"""
    factory, (room_id, player_id) = room
    async with factory() as db:
        await record_revelations(
            db, room_id=room_id, player_id=player_id, fact_ids=["fact-001"], via="check"
        )
        await db.commit()
    async with factory() as db:
        fresh = await record_revelations(
            db, room_id=room_id, player_id=player_id, fact_ids=["fact-001"], via="node"
        )
        await db.commit()
    assert fresh == []
    async with factory() as db:
        assert await revealed_fact_ids(db, room_id=room_id) == {"fact-001"}


@pytest.mark.asyncio
async def test_ledger_survives_the_history_window(room) -> None:
    """🔴 P4 的核心断言：第 3 轮拿到的线索，在 300+ 事件之后仍然读得到。

    历史重放是 `HISTORY_LIMIT`（200）条的**滑动窗口**，几十小时的战役会把
    开头挤出去。账本读全量、不设 limit，正是为了不受它影响。
    """
    factory, (room_id, player_id) = room

    # 第 3 轮：拿到一条线索
    async with factory() as db:
        await record_revelations(
            db, room_id=room_id, player_id=player_id, fact_ids=["fact-001"], via="check"
        )
        await db.commit()

    # 之后灌 300 条普通事件，远超窗口
    async with factory() as db:
        for i in range(300):
            db.add(
                Event(
                    room_id=room_id,
                    player_id=player_id,
                    event_type="action.submit",
                    payload={"utterance": f"第 {i} 轮的发言"},
                )
            )
        await db.commit()

    async with factory() as db:
        known = await revealed_fact_ids(db, room_id=room_id)
    assert "fact-001" in known
    assert EARLY_CLUE in render_ledger(_module(), known)


@pytest.mark.asyncio
async def test_history_window_really_would_have_dropped_it(room) -> None:
    """反证：同一批事件下，按窗口取最近 200 条确实已经看不到最早那条。

    没有这条对照，上面那个断言可能只是"窗口没满"而不是"账本起了作用"。
    """
    from sqlalchemy import select

    factory, (room_id, player_id) = room
    async with factory() as db:
        db.add(
            Event(
                room_id=room_id,
                player_id=player_id,
                event_type="action.submit",
                payload={"utterance": "最早的一句"},
            )
        )
        await db.commit()
    async with factory() as db:
        for i in range(300):
            db.add(
                Event(
                    room_id=room_id,
                    player_id=player_id,
                    event_type="action.submit",
                    payload={"utterance": f"第 {i} 轮"},
                )
            )
        await db.commit()

    async with factory() as db:
        rows = await db.execute(
            select(Event.payload)
            .where(Event.room_id == room_id, Event.event_type == "action.submit")
            .order_by(Event.created_at.desc(), Event.id.desc())
            .limit(HISTORY_LIMIT)
        )
        window = [(p or {}).get("utterance") for (p,) in rows]
    assert "最早的一句" not in window


def test_render_ledger_never_shows_meta_facts() -> None:
    """元层不可能被"揭开"，也绝不该渲染进上下文。"""
    text = render_ledger(_module(), {"fact-001", "fact-meta"})
    assert EARLY_CLUE in text
    assert "真凶其实是管家" not in text


def test_empty_ledger_renders_nothing() -> None:
    """短模组开局账本为空 → 整块省略，输出不变脏（退化证明的一半）。"""
    assert render_ledger(_module(), set()) == ""


def test_ledger_block_is_injected_into_the_situation() -> None:
    """账本必须真的进上下文——否则记了账也没用。

    变异检验发现这条接线原本没测试守着：把 ledger_block 从返回串里删掉，
    全部用例照样绿。
    """
    from app.core.keeper.narration.prompts import format_turn_input

    with_ledger = format_turn_input(
        None, [], [], "阿福", "我四处看看", ledger_status=f"- {EARLY_CLUE}"
    )
    assert EARLY_CLUE in with_ledger
    assert "已确认的线索" in with_ledger


def test_empty_ledger_leaves_the_situation_untouched() -> None:
    """🔴 退化证明：短模组账本为空 → 局面块与加这个功能之前逐字一致。"""
    from app.core.keeper.narration.prompts import format_turn_input

    baseline = format_turn_input(None, [], [], "阿福", "我四处看看")
    assert "已确认的线索" not in baseline
    assert format_turn_input(None, [], [], "阿福", "我四处看看", ledger_status="") == baseline
