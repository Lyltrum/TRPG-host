"""两段式玩家掷骰：待掷检定队列（app/core/keeper/runtime/pending.py）+
`KeeperAgent.resolve_check`（app/core/keeper/runtime/agent.py）。

`PendingDecisionManager` 的单测不碰数据库/LLM，纯内存结构断言。
`resolve_check` 的单测需要真实 DB 写入（服务端权威掷骰要落库/改角色卡），
但不跑真实 LLM——队列还没清空的路径本来就不涉及 LLM；队列清空触发的
"结算叙事"路径用 `_StubKeeperAgent` 桩掉 `narrate()`，只断言 resolve_check
自己如何合并 check_results，不依赖网络请求。
"""

import random
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.memory.fact_ledger import revealed_fact_ids
from app.core.keeper.runtime.agent import KeeperAgent
from app.core.keeper.runtime.deps import KeeperDeps, KeeperToolError
from app.core.keeper.runtime.pending import (
    ROLL_KINDS,
    PendingDecision,
    PendingDecisionManager,
    pending_decision_manager,
    to_notice,
)
from app.core.narration.contract import CheckResultNotice, NarrationContext, NarrationOutcome
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
    # 队列现在**在数据库里**（exec/24 §8.1），drop_all 就是隔离，不再需要手动
    # 清进程内存单例。


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


# ── PendingDecisionManager：纯内存结构 ──────────────────


def _check(
    room_id: str = "room-1", check_request_id: str = "chk-1", **overrides
) -> PendingDecision:
    # 值类型是异质的（str / tuple），`**` 展开时 ty 推不出各字段的具体类型。
    defaults: dict[str, object] = {
        "decision_id": check_request_id,
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
    return PendingDecision.roll(**defaults)  # ty: ignore[invalid-argument-type]


async def _bare_room(code: str) -> tuple[str, str]:
    """只要房间 + 一名玩家——队列那两列是真外键，`player_id` 还是 Uuid 列。"""
    async with _session_factory() as db:
        room = Room(room_code=code, room_name="队列测试房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="阿福")
        db.add(player)
        await db.commit()
        return room.id, player.id


async def test_manager_add_first_has() -> None:
    room_id, player_id = await _bare_room("QUEUE1")
    manager = PendingDecisionManager()
    async with _session_factory() as db:
        assert await manager.first(db, room_id, ROLL_KINDS) is None
        assert await manager.has(db, room_id, ROLL_KINDS) is False

        c1 = _check(room_id=room_id, player_id=player_id, check_request_id="chk-1")
        c2 = _check(room_id=room_id, player_id=player_id, check_request_id="chk-2")
        await manager.add(db, room_id, [c1, c2])
        await db.commit()

    async with _session_factory() as db:
        assert await manager.has(db, room_id, ROLL_KINDS) is True
        first = await manager.first(db, room_id, ROLL_KINDS)
        # 先进先出：排序靠自增 seq，不是 created_at——同一轮挂起的多个检定
        # 毫秒级时间戳分不出先后
        assert first is not None and first.decision_id == "chk-1"


async def test_manager_add_empty_list_is_noop() -> None:
    room_id, _player_id = await _bare_room("QUEUE2")
    manager = PendingDecisionManager()
    async with _session_factory() as db:
        await manager.add(db, room_id, [])
        assert await manager.has(db, room_id, ROLL_KINDS) is False


async def test_manager_pop_by_id_and_queue_isolation() -> None:
    room_a, player_a = await _bare_room("QUEUE3")
    room_b, player_b = await _bare_room("QUEUE4")
    manager = PendingDecisionManager()
    async with _session_factory() as db:
        await manager.add(
            db, room_a, [_check(room_id=room_a, player_id=player_a, check_request_id="chk-1")]
        )
        await manager.add(
            db, room_b, [_check(room_id=room_b, player_id=player_b, check_request_id="chk-2")]
        )
        await db.commit()

    async with _session_factory() as db:
        assert await manager.pop(db, room_a, "not-an-id") is None  # 找不到不炸
        popped = await manager.pop(db, room_a, "chk-1")
        assert popped is not None and popped.decision_id == "chk-1"
        await db.commit()

    async with _session_factory() as db:
        assert await manager.has(db, room_a, ROLL_KINDS) is False
        assert await manager.has(db, room_b, ROLL_KINDS) is True  # 不影响其它房间


async def test_queue_survives_a_process_restart() -> None:
    """🔴 这条就是落库的全部理由（exec/24 §8.1）。

    队列原先在进程内存里：后端一重启就清空，而 `narrate` 有 pending 守卫，
    玩家等的那张检定卡片永远不会来、守秘人一直回「请先完成待掷的检定」，
    **整局死锁且没有出路**（重发行动也撞在守卫上）。

    这里用「换一个全新的 manager 实例 + 全新的 session」模拟重启——manager
    现在无实例状态，能读回来就说明状态真的不在进程里。
    """
    room_id, player_id = await _bare_room("QUEUE5")
    async with _session_factory() as db:
        await PendingDecisionManager().add(
            db,
            room_id,
            [_check(room_id=room_id, player_id=player_id, check_request_id="chk-survive")],
        )
        await db.commit()

    async with _session_factory() as db:
        survived = await PendingDecisionManager().first(db, room_id, ROLL_KINDS)
    assert survived is not None
    assert survived.decision_id == "chk-survive"
    # 结构化字段要原样活过一个来回，不能只剩个 id
    assert survived.reveals == ()
    assert survived.kind == "skill"


async def _enqueue(room_id: str, checks: list[PendingDecision]) -> None:
    """把检定挂进队列（队列已落库，exec/24 §8.1）。"""
    async with _session_factory() as db:
        await pending_decision_manager.add(db, room_id, checks)
        await db.commit()


# ── KeeperAgent.resolve_check ────────────────────────


async def test_resolve_check_unknown_id_raises() -> None:
    room_id, player_id, _nickname = await _seed_room()
    with pytest.raises(KeeperToolError, match="没有这个待掷的检定"):
        await _agent().resolve_check(room_id, player_id, "no-such-id")


async def test_resolve_check_wrong_player_raises_and_requeues() -> None:
    room_id, player_id, nickname = await _seed_room()
    check_request_id = "chk-wrong-player"
    await _enqueue(
        room_id,
        [_check(room_id=room_id, check_request_id=check_request_id, player_id=player_id)],
    )

    with pytest.raises(KeeperToolError, match=nickname):
        await _agent().resolve_check(room_id, "someone-else", check_request_id)

    # 检定仍然待掷——错玩家掷不能让它凭空消失。
    async with _session_factory() as db:
        still_pending = await pending_decision_manager.first(db, room_id, ROLL_KINDS)
    assert still_pending is not None
    assert still_pending.decision_id == check_request_id


async def test_resolve_check_queue_not_empty_only_broadcasts_result() -> None:
    """队列里还有下一个待掷检定时：只结算这一个，不叙事（text==""），
    check_requests 带下一个的通知，不触碰 LLM。"""
    room_id, player_id, nickname = await _seed_room()
    first_id, second_id = "chk-first", "chk-second"
    await _enqueue(
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
    async with _session_factory() as db:
        next_pending = await pending_decision_manager.first(db, room_id, ROLL_KINDS)
    assert next_pending is not None
    assert next_pending.decision_id == second_id


async def test_resolve_check_san_result_fields() -> None:
    room_id, player_id, nickname = await _seed_room()
    check_request_id = "chk-san"
    await _enqueue(
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
    await _enqueue(
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
    async with _session_factory() as db:
        assert await pending_decision_manager.has(db, room_id, ROLL_KINDS) is False


# ── 事实账本接线（exec/14 P4）──────────────────────────────


class _FixedRoll(random.Random):
    """固定 d100 结果，用来精确造出"成功"或"失败"。"""

    def __init__(self, value: int) -> None:
        super().__init__()
        self._value = value

    def randint(self, a: int, b: int) -> int:  # noqa: D102
        return self._value if (a, b) == (1, 100) else super().randint(a, b)


async def _resolve_with_roll(roll: int, reveals: tuple[str, ...]) -> tuple[str, NarrationOutcome]:
    room_id, player_id, nickname = await _seed_room()
    await _enqueue(
        room_id,
        [
            _check(
                room_id=room_id,
                check_request_id="chk-ledger",
                player_id=player_id,
                player_nickname=nickname,
                skill="侦察",
                reveals=reveals,
            )
        ],
    )
    agent = _StubKeeperAgent(
        api_key="fake-key",
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
        stub_outcome=NarrationOutcome(text="结算叙事"),
        rng=_FixedRoll(roll),
    )
    outcome = await agent.resolve_check(room_id, player_id, "chk-ledger")
    return room_id, outcome


async def test_successful_check_records_its_facts() -> None:
    room_id, _ = await _resolve_with_roll(1, ("fact-001",))  # 01 恒为大成功
    async with _session_factory() as db:
        assert await revealed_fact_ids(db, room_id=room_id) == {"fact-001"}


async def test_failed_check_records_nothing() -> None:
    """🔴 掷失败不该白拿线索——变异检验发现这条原本没有测试守着。"""
    room_id, _ = await _resolve_with_roll(100, ("fact-001",))  # 100 恒为大失败
    async with _session_factory() as db:
        assert await revealed_fact_ids(db, room_id=room_id) == set()


async def test_pending_check_carries_reveals_from_the_module() -> None:
    """🔴 模组标注的 reveals 必须绑到待掷记录上。

    变异检验发现这段接线原本没有测试守着：把绑定改成空元组，全部用例照样绿。
    绑定时机是"创建待掷记录时"而不是"结算时再查"——待掷期间场景可能已经变了。
    """
    from app.core.keeper.capabilities.skill_check.schema import CheckRequest
    from app.core.keeper.contract.decision import KeeperDecision
    from app.core.keeper.contract.module_loader import ModuleFact
    from app.core.keeper.runtime.turn_executor import create_pending_checks

    room_id, player_id, _nickname = await _seed_room()
    module = load_module(_FIXTURE_MODULE)
    hall = module.node_by_id("hall")
    assert hall is not None
    module.facts.append(ModuleFact(id="fact-001", text="地毯上有半干的泥脚印"))
    hall.checks[0].reveals = ["fact-001"]

    # 把房间定位到那个节点，护栏才会放行该节点标注的检定
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        room.keeper_state = {"当前场景": hall.title, "当前场景节点": hall.id}
        await db.commit()

    deps = KeeperDeps(
        room_id=room_id,
        player_id=player_id,
        session_factory=_session_factory,
        module=module,
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
    )
    pending, _issues = await create_pending_checks(
        deps, KeeperDecision(checks=[CheckRequest(skill_id=hall.checks[0].skill_ids[0])])
    )
    assert [p.reveals for p in pending] == [("fact-001",)]


# ── 骰值先落地、叙事随后（真人实测「反馈太慢」）─────────


async def test_on_result_fires_before_the_settlement_narration() -> None:
    """🔴 骰子一落地就回调，**不等**后面那次结算叙事。

    掷骰是纯代码毫秒级，结算叙事是 10 秒级的 LLM 往返。原来两件事跑完才一次性
    返回，WS 层只能等到最后才广播，玩家点完「投掷」得盯着屏幕十几秒才看得到
    自己掷了多少。真人桌上骰子是当场停下的。

    断言用的是**顺序**而不是"有没有调过"：调到了但排在叙事后面，等于没改。
    """
    room_id, player_id, nickname = await _seed_room()
    check_request_id = "chk-timing"
    await _enqueue(
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
    agent = _stub_agent(NarrationOutcome(text="结算叙事"))
    trace: list[str] = []

    original_narrate = agent.narrate

    async def _traced_narrate(context: NarrationContext) -> NarrationOutcome:
        trace.append("narrate")
        return await original_narrate(context)

    # 直接替实例上的方法，不改类——这条用例要的是"调用顺序"，不是新行为
    object.__setattr__(agent, "narrate", _traced_narrate)

    async def _on_result(notice: CheckResultNotice) -> None:
        trace.append(f"result:{notice.check_request_id}")

    outcome = await agent.resolve_check(room_id, player_id, check_request_id, _on_result)

    assert trace == [f"result:{check_request_id}", "narrate"]
    # 结果仍然留在返回值里：没用回调的老调用方行为不变
    assert [r.check_request_id for r in outcome.check_results] == [check_request_id]


async def test_on_result_fires_even_when_more_checks_are_pending() -> None:
    """队列没清空的分支（不走结算叙事）也要回调——否则多人连掷时只有最后
    一个人的骰子是"秒出"的。"""
    room_id, player_id, nickname = await _seed_room()
    await _enqueue(
        room_id,
        [
            _check(
                room_id=room_id,
                check_request_id="chk-a",
                player_id=player_id,
                player_nickname=nickname,
            ),
            _check(
                room_id=room_id,
                check_request_id="chk-b",
                player_id=player_id,
                player_nickname=nickname,
            ),
        ],
    )
    seen: list[str] = []

    async def _on_result(notice: CheckResultNotice) -> None:
        seen.append(notice.check_request_id)

    await _agent().resolve_check(room_id, player_id, "chk-a", _on_result)

    assert seen == ["chk-a"]


# ── 重连补发（真人实测 exec/23 #56）────────────────


async def test_reconnect_resends_pending_checks() -> None:
    """🔴 队列落库只解决了一半。

    `check.request` 只在裁决那一刻**实时推过一次**。刷新页面或重启后端之后，
    队列里那条检定还在（这是 §8.1 保证的），但客户端再也收不到卡片——而
    `narrate` 的守卫会一直挡住新一轮，对局停在"守秘人等你掷骰、你屏幕上却
    没有骰子"的死角。真人实测当场复现。

    这里断言的是**服务端能把队列重新讲出来**：`list_all` + `to_notice` 就是
    重连握手用的那条路径（WS 侧的接线由 e2e 覆盖，pytest 起不了两个客户端）。
    """
    room_id, player_id = await _bare_room("QUEUE6")
    await _enqueue(
        room_id,
        [
            _check(room_id=room_id, player_id=player_id, check_request_id="chk-1"),
            _check(
                room_id=room_id,
                player_id=player_id,
                check_request_id="chk-2",
                kind="san",
                skill=None,
            ),
        ],
    )

    async with _session_factory() as db:
        pendings = await pending_decision_manager.list_all(db, room_id)

    notices = [to_notice(p) for p in pendings]
    # 顺序要保住：谁先掷是有意义的
    assert [n.check_request_id for n in notices] == ["chk-1", "chk-2"]
    # 两种检定各自还原成对的事件类型所需的字段
    assert notices[0].kind == "skill" and notices[0].skill == "侦察"
    assert notices[1].kind == "san" and notices[1].skill is None
    assert all(n.player_nickname == "阿福" for n in notices)
