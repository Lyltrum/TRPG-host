"""分段摘要 L2（exec/14 P4.2）。

L1 保证"事实不丢"，L2 承担"记得大概就行"的那部分——跟谁翻过脸、许过什么诺、
哪扇门被撞坏了。判据：**"必须记住"的走 L1，"记得大概就行"的走 L2。**
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import Base
from app.core.keeper.memory.chapter import (
    CHAPTER_MAX_CHARS,
    MIN_TURNS_BETWEEN_CHAPTERS,
    build_recap,
    load_chapters,
    record_chapter,
    render_chapters,
    should_summarize,
    turns_since_last_chapter,
)
from app.models.event import Event
from app.models.room import Player, Room


@pytest.fixture
async def room(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/chapter.db", poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as db:
        r = Room(room_code="CHAPT1", room_name="摘要房", max_players=4, phase="InGame")
        db.add(r)
        await db.flush()
        p = Player(room_id=r.id, nickname="调查员甲", is_host=True)
        db.add(p)
        await db.commit()
        ids = (r.id, p.id)
    yield factory, ids
    await engine.dispose()


async def _add_actions(factory, room_id: str, player_id: str, count: int) -> None:
    async with factory() as db:
        for i in range(count):
            db.add(
                Event(
                    room_id=room_id,
                    player_id=player_id,
                    event_type="action.submit",
                    payload={"utterance": f"第 {i} 轮"},
                )
            )
        await db.commit()


# ── 触发条件：两者取交集 ─────────────────────────────────────


def test_scene_change_alone_is_not_enough() -> None:
    """只按场景切换会在玩家来回踱步时疯狂触发。"""
    assert should_summarize(scene_changed=True, turns_since_last=1) is False


def test_turns_alone_is_not_enough() -> None:
    """只按轮数会把一段完整的戏拦腰截断。"""
    assert should_summarize(scene_changed=False, turns_since_last=999) is False


def test_both_conditions_trigger() -> None:
    assert should_summarize(scene_changed=True, turns_since_last=MIN_TURNS_BETWEEN_CHAPTERS) is True


# ── 计数：从上一段摘要之后算起 ───────────────────────────────


@pytest.mark.asyncio
async def test_turn_count_resets_after_each_chapter(room) -> None:
    factory, (room_id, player_id) = room
    await _add_actions(factory, room_id, player_id, 5)
    async with factory() as db:
        assert await turns_since_last_chapter(db, room_id=room_id) == 5

    async with factory() as db:
        await record_chapter(db, room_id=room_id, text="调查员搜完了门厅，去了地下室。")
        await db.commit()

    # 摘要之后重新计数，不该把摘要之前的轮次再算一遍
    async with factory() as db:
        assert await turns_since_last_chapter(db, room_id=room_id) == 0
    await _add_actions(factory, room_id, player_id, 3)
    async with factory() as db:
        assert await turns_since_last_chapter(db, room_id=room_id) == 3


# ── 存储与渲染 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chapters_are_ordered_and_unbounded(room) -> None:
    """不设 limit——与 L1 同理，必须活过 L3 的 200 条窗口。"""
    factory, (room_id, player_id) = room
    async with factory() as db:
        for i in range(3):
            await record_chapter(db, room_id=room_id, text=f"第 {i} 段")
        await db.commit()
    await _add_actions(factory, room_id, player_id, 300)

    async with factory() as db:
        chapters = await load_chapters(db, room_id=room_id)
    assert chapters == ["第 0 段", "第 1 段", "第 2 段"]


@pytest.mark.asyncio
async def test_overlong_summary_is_truncated(room) -> None:
    """摘要要长期常驻上下文，不能自己变成新的上下文负担。"""
    factory, (room_id, _player_id) = room
    async with factory() as db:
        await record_chapter(db, room_id=room_id, text="很长" * 200)
        await db.commit()
    async with factory() as db:
        assert len((await load_chapters(db, room_id=room_id))[0]) <= CHAPTER_MAX_CHARS


@pytest.mark.asyncio
async def test_blank_summary_is_not_recorded(room) -> None:
    """LLM 返回空时不该留一条空摘要占位。"""
    factory, (room_id, _player_id) = room
    async with factory() as db:
        await record_chapter(db, room_id=room_id, text="   ")
        await db.commit()
    async with factory() as db:
        assert await load_chapters(db, room_id=room_id) == []


def test_empty_chapters_render_nothing() -> None:
    """退化证明：短模组没有摘要 → 整块省略，局面块不变脏。"""
    assert render_chapters([]) == ""


# ── 上集回顾 ─────────────────────────────────────────────────


def test_recap_combines_chapters_and_ledger() -> None:
    recap = build_recap(["去了地下室，撬开了木箱。"], "- 箱底压着一张船票")
    assert "地下室" in recap
    assert "船票" in recap


def test_recap_is_empty_when_nothing_happened_yet() -> None:
    assert build_recap([], "") == ""


# ── agent 接线：离线生成本身 ─────────────────────────────────


class _CountingClient:
    """假 LLM 客户端：记调用次数，返回固定摘要。"""

    def __init__(self) -> None:
        self.calls = 0
        self.chat = type("_Chat", (), {"completions": self})()

    async def create(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls += 1
        from dataclasses import dataclass

        @dataclass
        class _M:
            content: str

        @dataclass
        class _C:
            message: _M
            finish_reason: str = "stop"

        @dataclass
        class _R:
            choices: list

        return _R(choices=[_C(message=_M("调查员离开门厅，下到地下室。"))])


def _agent_with(factory, client):
    from app.core.coc7_content import build_coc7_ruleset
    from app.core.keeper.contract.module_loader import load_module
    from app.core.keeper.runtime.agent import KeeperAgent

    agent = KeeperAgent(
        api_key="fake",
        module=load_module(Path(__file__).parent / "fixtures" / "keeper_module.json"),
        ruleset=build_coc7_ruleset(),
        session_factory=factory,
    )
    agent._client = client
    return agent


@pytest.mark.asyncio
async def test_summary_is_skipped_when_too_few_turns(room) -> None:
    """轮数不够时连模型都不该调——省钱也省延迟。"""
    factory, (room_id, player_id) = room
    await _add_actions(factory, room_id, player_id, MIN_TURNS_BETWEEN_CHAPTERS - 1)
    client = _CountingClient()
    await _agent_with(factory, client)._summarize_chapter(room_id, ["阿福：我下楼"])
    assert client.calls == 0
    async with factory() as db:
        assert await load_chapters(db, room_id=room_id) == []


@pytest.mark.asyncio
async def test_summary_is_generated_and_stored_when_due(room) -> None:
    factory, (room_id, player_id) = room
    await _add_actions(factory, room_id, player_id, MIN_TURNS_BETWEEN_CHAPTERS)
    client = _CountingClient()
    await _agent_with(factory, client)._summarize_chapter(room_id, ["阿福：我下楼"])
    assert client.calls == 1
    async with factory() as db:
        assert await load_chapters(db, room_id=room_id) == ["调查员离开门厅，下到地下室。"]


@pytest.mark.asyncio
async def test_summary_failure_never_breaks_the_turn(room) -> None:
    """它是记忆的锦上添花，不是主路径——LLM 炸了也只记日志。"""
    factory, (room_id, player_id) = room
    await _add_actions(factory, room_id, player_id, MIN_TURNS_BETWEEN_CHAPTERS)

    class _Boom(_CountingClient):
        async def create(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("模型挂了")

    await _agent_with(factory, _Boom())._summarize_chapter(room_id, ["阿福：我下楼"])
    async with factory() as db:
        assert await load_chapters(db, room_id=room_id) == []


@pytest.mark.asyncio
async def test_background_task_reference_is_retained(room) -> None:
    """asyncio 只持弱引用——不自己存一份，任务可能被 GC 提前回收。"""
    factory, (room_id, player_id) = room
    agent = _agent_with(factory, _CountingClient())
    agent._spawn_chapter_summary(room_id, ["阿福：我下楼"])
    assert len(agent._background) == 1
    for task in list(agent._background):
        await task
