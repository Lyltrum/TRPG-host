"""两段式玩家掷骰：待掷检定队列（app/core/keeper/pending.py）+
`KeeperAgent.resolve_check`（app/core/keeper/agent.py）。

`PendingCheckManager` 的单测不碰数据库/LLM，纯内存结构断言。
`resolve_check` 的单测需要真实 DB 写入（服务端权威掷骰要落库/改角色卡），
但不跑真实 LLM——队列还没清空的路径本来就不涉及 LLM；队列清空触发的
"结算叙事"路径用 `_StubKeeperAgent` 桩掉 `narrate()`，只断言 resolve_check
自己如何合并 check_results，不依赖网络请求。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.agent import KeeperAgent
from app.core.keeper.module_loader import load_module
from app.core.keeper.pending import PendingCheck, PendingCheckManager, pending_check_manager
from app.core.keeper.tools import KeeperToolError
from app.core.narrator import CheckResultNotice, NarrationContext, NarrationOutcome
from app.models.room import Character, Player, Room

_FIXTURE_MODULE = Path(__file__).parent / "fixtures" / "keeper_module.json"

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-pending-test-")) / "pending.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    # `pending_check_manager` 是进程内存单例，不随 DB 一起清空——测试用例
    # 之间必须手动隔离，否则一个用例遗留的队列会泄漏进下一个。
    pending_check_manager._queues.clear()


class _StubKeeperAgent(KeeperAgent):
    """resolve_check 队列清空后的"结算叙事"路径会调用 `self.narrate(...)`
    触发下一轮裁决——这里桩掉它，断言只关心 resolve_check 自己如何合并
    check_results，不需要真的跑一轮裁决/叙事 LLM 调用。"""

    def __init__(self, *args, stub_outcome: NarrationOutcome, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._stub_outcome = stub_outcome
        self.narrate_calls: list[NarrationContext] = []

    async def narrate(self, context: NarrationContext) -> NarrationOutcome:
        self.narrate_calls.append(context)
        return self._stub_outcome


async def _seed_room() -> tuple[str, str, str]:
    """建一个房间 + 一名带角色卡的玩家。返回 (room_id, player_id, nickname)。"""
    async with _session_factory() as db:
        room = Room(room_code="PEND01", room_name="待掷测试房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="阿福")
        db.add(player)
        await db.flush()
        db.add(
            Character(
                room_id=room.id,
                player_id=player.id,
                status="complete",
                name="侦探福",
                occupation="私家侦探",
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
                },  # fmt: skip
                derived_stats={"HP": 10, "MP": 10, "SAN": 50, "MOV": 8},
                skills={"spot-hidden": 70},
            )
        )
        await db.commit()
        return room.id, player.id, player.nickname


def _agent() -> KeeperAgent:
    return KeeperAgent(
        api_key="fake-key",
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
    )


def _stub_agent(stub_outcome: NarrationOutcome) -> _StubKeeperAgent:
    return _StubKeeperAgent(
        api_key="fake-key",
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
        stub_outcome=stub_outcome,
    )


# ── PendingCheckManager：纯内存结构 ──────────────────


def _check(room_id: str = "room-1", check_request_id: str = "chk-1", **overrides) -> PendingCheck:
    defaults = {
        "check_request_id": check_request_id,
        "kind": "skill",
        "room_id": room_id,
        "player_id": "player-1",
        "player_nickname": "阿福",
        "skill": "侦察",
        "loss_on_success": "0",
        "loss_on_failure": "0",
        "reason": "搜索书房",
    }
    defaults.update(overrides)
    return PendingCheck(**defaults)


def test_manager_add_first_has() -> None:
    manager = PendingCheckManager()
    assert manager.first("room-1") is None
    assert manager.has("room-1") is False

    c1 = _check(check_request_id="chk-1")
    c2 = _check(check_request_id="chk-2")
    manager.add("room-1", [c1, c2])

    assert manager.has("room-1") is True
    assert manager.first("room-1") is c1  # 先进先出


def test_manager_add_empty_list_is_noop() -> None:
    manager = PendingCheckManager()
    manager.add("room-1", [])
    assert manager.has("room-1") is False


def test_manager_pop_by_id_and_queue_isolation() -> None:
    manager = PendingCheckManager()
    manager.add("room-1", [_check(room_id="room-1", check_request_id="chk-1")])
    manager.add("room-2", [_check(room_id="room-2", check_request_id="chk-2")])

    assert manager.pop("room-1", "not-an-id") is None  # 找不到不炸
    popped = manager.pop("room-1", "chk-1")
    assert popped is not None and popped.check_request_id == "chk-1"
    assert manager.has("room-1") is False  # 弹空后队列本身也被清理
    assert manager.has("room-2") is True  # 不影响其它房间


def test_manager_requeue_front() -> None:
    manager = PendingCheckManager()
    c1 = _check(check_request_id="chk-1")
    c2 = _check(check_request_id="chk-2")
    manager.add("room-1", [c1])
    popped = manager.pop("room-1", "chk-1")
    assert popped is not None
    manager.add("room-1", [c2])  # 模拟"掷错玩家"发生时队列里还有别的检定
    manager.requeue_front("room-1", popped)

    assert [c.check_request_id for c in manager._queues["room-1"]] == ["chk-1", "chk-2"]


# ── KeeperAgent.resolve_check ────────────────────────


async def test_resolve_check_unknown_id_raises() -> None:
    room_id, player_id, _nickname = await _seed_room()
    with pytest.raises(KeeperToolError, match="没有这个待掷的检定"):
        await _agent().resolve_check(room_id, player_id, "no-such-id")


async def test_resolve_check_wrong_player_raises_and_requeues() -> None:
    room_id, player_id, nickname = await _seed_room()
    check_request_id = "chk-wrong-player"
    pending_check_manager.add(
        room_id,
        [_check(room_id=room_id, check_request_id=check_request_id, player_id=player_id)],
    )

    with pytest.raises(KeeperToolError, match=nickname):
        await _agent().resolve_check(room_id, "someone-else", check_request_id)

    # 检定仍然待掷——错玩家掷不能让它凭空消失。
    still_pending = pending_check_manager.first(room_id)
    assert still_pending is not None
    assert still_pending.check_request_id == check_request_id


async def test_resolve_check_queue_not_empty_only_broadcasts_result() -> None:
    """队列里还有下一个待掷检定时：只结算这一个，不叙事（text==""），
    check_requests 带下一个的通知，不触碰 LLM。"""
    room_id, player_id, nickname = await _seed_room()
    first_id, second_id = "chk-first", "chk-second"
    pending_check_manager.add(
        room_id,
        [
            _check(
                room_id=room_id,
                check_request_id=first_id,
                player_id=player_id,
                player_nickname=nickname,
                skill="侦察",
            ),
            _check(
                room_id=room_id,
                check_request_id=second_id,
                player_id=player_id,
                player_nickname=nickname,
                kind="san",
                skill=None,
            ),
        ],
    )

    outcome = await _agent().resolve_check(room_id, player_id, first_id)

    assert outcome.text == ""
    assert len(outcome.check_results) == 1
    result = outcome.check_results[0]
    assert result.check_request_id == first_id
    assert result.kind == "skill"
    assert result.player_id == player_id
    assert 1 <= result.rolled <= 100
    assert result.target == 70  # spot-hidden 总值

    assert len(outcome.check_requests) == 1
    assert outcome.check_requests[0].check_request_id == second_id

    # 第一个已经被弹出，第二个还在队列里等着。
    next_pending = pending_check_manager.first(room_id)
    assert next_pending is not None
    assert next_pending.check_request_id == second_id


async def test_resolve_check_san_result_fields() -> None:
    room_id, player_id, nickname = await _seed_room()
    check_request_id = "chk-san"
    pending_check_manager.add(
        room_id,
        [
            _check(
                room_id=room_id,
                check_request_id=check_request_id,
                player_id=player_id,
                player_nickname=nickname,
                kind="san",
                skill=None,
                loss_on_success="0",
                loss_on_failure="1d6",
            ),
            # 第二项保证队列不清空，走"只广播结果"分支，不涉及 LLM。
            _check(
                room_id=room_id,
                check_request_id="chk-followup",
                player_id=player_id,
                player_nickname=nickname,
            ),
        ],
    )

    outcome = await _agent().resolve_check(room_id, player_id, check_request_id)

    result = outcome.check_results[0]
    assert result.kind == "san"
    assert result.skill is None
    assert result.level in ("成功", "失败")
    assert result.san_loss is not None
    assert result.san_remaining is not None


async def test_resolve_check_queue_empty_triggers_settlement_narration() -> None:
    """队列清空后复用 narrate() 触发结算叙事（这里桩掉，只断言合并顺序：
    刚结算的这次结果排在最前面，其余是 narrate() 桩返回的）。"""
    room_id, player_id, nickname = await _seed_room()
    check_request_id = "chk-only"
    pending_check_manager.add(
        room_id,
        [
            _check(
                room_id=room_id,
                check_request_id=check_request_id,
                player_id=player_id,
                player_nickname=nickname,
            )
        ],
    )
    stub_notice = CheckResultNotice(
        check_request_id="chained-san",
        kind="san",
        player_id=player_id,
        skill=None,
        rolled=50,
        target=50,
        level="失败",
        san_loss=3,
        san_remaining=47,
    )
    stub_outcome = NarrationOutcome(text="你看清了那东西……", check_results=[stub_notice])
    agent = _stub_agent(stub_outcome)

    outcome = await agent.resolve_check(room_id, player_id, check_request_id)

    assert outcome.text == "你看清了那东西……"
    assert [r.check_request_id for r in outcome.check_results] == [check_request_id, "chained-san"]
    assert len(agent.narrate_calls) == 1
    assert agent.narrate_calls[0].utterance == "（掷骰完成，请根据检定结果继续）"
    assert pending_check_manager.has(room_id) is False
