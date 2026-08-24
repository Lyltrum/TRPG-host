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
from app.core.keeper.runtime.agent import ROLL_PENDING_NOTICE, KeeperAgent
from app.core.keeper.runtime.deps import KeeperDeps, KeeperToolError
from app.core.keeper.runtime.pending import (
    LUCK_SPEND_KIND,
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


#: 🔴 掷骰种子必须钉死（`exec/34` 第 4 步之后）：`spot-hidden` 总值 70，掷出
#: 71–80 就会停下来问「花幸运吗」——而这个文件里大多数用例的前提是"结算一路走到
#: 底"。用随机 rng 的话它们会**约 10% 的运行里失败，且失败长得像被测对象的问题**。
#: 这个种子的前几掷是 50/98/54，都不在那个区间里。要验幸运那一拍的用例自己传
#: `_NEAR_MISS_SEED`。同族于「测试必须钉死所有参与选择逻辑的环境字段」。
_NO_OFFER_SEED = 0


def _agent(rng: random.Random | None = None) -> KeeperAgent:
    return KeeperAgent(
        api_key="fake-key",
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
        rng=rng if rng is not None else random.Random(_NO_OFFER_SEED),
    )


def _stub_agent(stub_outcome: NarrationOutcome) -> _StubKeeperAgent:
    return _StubKeeperAgent(
        api_key="fake-key",
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
        rng=random.Random(_NO_OFFER_SEED),
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


async def test_an_improvised_check_rolls_but_carries_no_reveals() -> None:
    """🔴 护栏改版（2026-08-15）的正题：**照掷，但揭不开模组事实**。

    这是「模组标注的检定拿得到 reveals」那条的镜面。两条一起才说得清护栏
    现在到底在拦什么：拦的是**揭示权**，不是掷骰权。

    回归实测的病根就是它拦错了维度——玩家在只标了 `INT`/`LUCK` 的节点上说
    "追踪它/躲起来/辨方向"，`track`/`stealth`/`navigation` 被整条丢弃、
    玩家侧完全静默。而"即兴掷一把就把真相挖出来"这件真正要防的事，靠
    reveals 为空就已经防住了。
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
    whitelisted = hall.checks[0].skill_ids[0]

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
    improvised = "library-use"
    assert improvised != whitelisted, "样本必须真的落在白名单之外，否则这条什么都没测"
    pending, issues = await create_pending_checks(
        deps, KeeperDecision(checks=[CheckRequest(skill_id=improvised)])
    )

    assert len(pending) == 1, "即兴检定必须照样掷得出来"
    assert pending[0].reveals == (), "但它揭不开模组标注的事实"
    assert any("揭不开模组事实" in issue for issue in issues), issues


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


# ── 幸运消费那一拍（exec/34 第 4 步） ──────────────────


#: `spot-hidden` 总值 70，这个种子第一掷是 80 → 失败、差 10 点，正好落在阈值上。
_NEAR_MISS_SEED = 5


async def test_a_near_miss_pauses_before_the_effects_land() -> None:
    """🔴 这是拆掷骰与生效的**全部理由**：卡片挂着的时候，这次检定还**没有生效**。

    断言的是"世界上什么都没变"（events 为空），不是某个函数被调用过——
    副作用要是提前落地了，玩家花完幸运就得逐个回滚。
    """
    room_id, player_id, nickname = await _seed_room()
    await _enqueue(
        room_id,
        [
            _check(
                room_id=room_id,
                check_request_id="chk-luck",
                player_id=player_id,
                player_nickname=nickname,
                skill="侦察",
            )
        ],
    )

    outcome = await _agent(random.Random(_NEAR_MISS_SEED)).resolve_check(
        room_id, player_id, "chk-luck"
    )

    assert outcome.text == ""
    assert len(outcome.player_offers) == 1, "差 10 点、幸运 55，该问一句"
    assert outcome.player_offers[0].kind == "luck_spend"
    # 卡片要**真的落库**了才算数：玩家可能刷新页面，重连补发靠的是队列里那一行。
    async with _session_factory() as db:
        queued = await pending_decision_manager.first(db, room_id, {LUCK_SPEND_KIND})
    assert queued is not None and queued.cost == 10
    assert outcome.check_results[0].level == "失败", "骰子照常先广播——它已经停下了"

    async with _session_factory() as db:
        from sqlalchemy import func, select

        from app.models.event import Event

        events = (
            await db.execute(select(func.count()).select_from(Event).filter_by(room_id=room_id))
        ).scalar_one()
    assert events == 0, "🔴 卡片还挂着，这次检定就已经记进历史了"


async def test_answering_the_card_lets_the_turn_continue() -> None:
    """答完（这里选不花）才走生效 → 结算叙事，跟直接结算走的是同一条尾巴。"""
    room_id, player_id, nickname = await _seed_room()
    await _enqueue(
        room_id,
        [
            _check(
                room_id=room_id,
                check_request_id="chk-luck2",
                player_id=player_id,
                player_nickname=nickname,
                skill="侦察",
            )
        ],
    )
    agent = _stub_agent(NarrationOutcome(text="他直起身，一无所获。"))
    agent._rng = random.Random(_NEAR_MISS_SEED)
    offer = (await agent.resolve_check(room_id, player_id, "chk-luck2")).player_offers[0]

    outcome = await agent.resolve_player_offer(room_id, player_id, offer.decision_id, False)

    assert outcome.text == "他直起身，一无所获。"
    assert outcome.check_results[0].level == "失败"
    async with _session_factory() as db:
        from sqlalchemy import func, select

        from app.models.event import Event

        events = (
            await db.execute(select(func.count()).select_from(Event).filter_by(room_id=room_id))
        ).scalar_one()
    assert events == 1, "答完之后才该落库"


async def test_the_card_blocks_a_new_turn_the_same_way_a_dice_roll_does() -> None:
    """🔴 幸运卡必须进 `TURN_BLOCKING_KINDS`：它挂着的时候有一次检定的结果悬而
    未决，放行就等于让世界跑在一个还没定的结果前面。

    重发的是**卡片本身**，不是 `check.request`——后者点下去会报「没有这个待掷的
    检定」（同族于「加一种 kind 就要检查每个逐个列出类别的消费方」）。
    """
    room_id, player_id, nickname = await _seed_room()
    await _enqueue(
        room_id,
        [
            _check(
                room_id=room_id,
                check_request_id="chk-luck3",
                player_id=player_id,
                player_nickname=nickname,
                skill="侦察",
            )
        ],
    )
    agent = _agent(random.Random(_NEAR_MISS_SEED))
    await agent.resolve_check(room_id, player_id, "chk-luck3")

    blocked = await agent.narrate(
        NarrationContext(
            utterance="我再翻一遍抽屉",
            player_nickname=nickname,
            room_id=room_id,
            player_id=player_id,
        )
    )

    assert blocked.check_requests == []
    assert len(blocked.player_offers) == 1
    assert blocked.player_offers[0].kind == "luck_spend"


async def _add_player(room_id: str, nickname: str) -> str:
    async with _session_factory() as db:
        player = Player(room_id=room_id, nickname=nickname)
        db.add(player)
        await db.commit()
        return player.id


async def test_the_guard_speaks_only_to_the_owner_of_the_card() -> None:
    """🔴 守卫那句是**第二人称祈使句**，只能发给卡的主人（`exec/23 #76`）。

    此前它走全房间的 `text`，于是手上没有卡的玩家也被要求「先把手上那张检定卡
    掷了」——而他点不出任何东西。
    """
    room_id, player_id, nickname = await _seed_room()
    await _enqueue(
        room_id,
        [_check(room_id=room_id, check_request_id="chk-solo", player_id=player_id)],
    )

    blocked = await _agent().narrate(
        NarrationContext(
            utterance="我去看看窗外",
            player_nickname=nickname,
            room_id=room_id,
            player_id=player_id,
        )
    )

    assert blocked.text == "", "祈使句不许再走全房间广播"
    assert len(blocked.segments) == 1, "只有他一个人发言，没有第二个受众"
    assert blocked.segments[0].text == ROLL_PENDING_NOTICE
    assert blocked.segments[0].audience == (player_id,)


async def test_the_others_get_told_why_nothing_happened() -> None:
    """🔴 被顶回来的其他发言者**不能什么都收不到**——说了话没有任何回应，玩家
    只会认为坏了（「按钮没有缓冲区」的同族）。他们收的是第三人称的说明，
    不带技能名/理由/点数（那些在卡片上，卡片按受众发）。"""
    room_id, owner_id, nickname = await _seed_room()
    other_id = await _add_player(room_id, "阿贵")
    await _enqueue(
        room_id,
        [_check(room_id=room_id, check_request_id="chk-duo", player_id=owner_id)],
    )

    blocked = await _agent().narrate(
        NarrationContext(
            utterance="我推门进去",
            player_nickname="阿贵",
            room_id=room_id,
            player_id=other_id,
            participant_ids=(owner_id, other_id),
        )
    )

    by_audience = {seg.audience: seg.text for seg in blocked.segments}
    assert by_audience[(owner_id,)] == ROLL_PENDING_NOTICE
    said = by_audience[(other_id,)]
    assert nickname in said, "得说清楚在等谁"
    assert "侦察" not in said and "搜索书房" not in said, "技能名/理由只在卡片上"
    assert ROLL_PENDING_NOTICE not in said


# ── 玩家用桌上的实体骰掷（`exec/46` B5）──────────────


async def test_the_reported_roll_is_the_one_that_counts() -> None:
    """🔴 报了出目就用他报的那个，**不再另外掷一次**。

    这是这条功能的全部意义：线下掷骰子是最有仪式感的动作，而此前玩家掷完只能
    无视桌上那颗、照着系统给的数字玩——骰子成了摆设。

    **规则权威没有让出去**：要不要检定、目标值多少、算不算成功，仍然全由后端判。
    让出的只有随机数。
    """
    room_id, player_id, nickname = await _seed_room()
    check_request_id = "chk-manual"
    await _enqueue(
        room_id,
        [
            _check(
                room_id=room_id,
                check_request_id=check_request_id,
                player_id=player_id,
                player_nickname=nickname,
                skill="侦察",
            )
        ],
    )

    notices: list = []

    async def _capture(notice) -> None:
        notices.append(notice)

    # 队列清空后会走结算叙事（一次真 LLM 往返）——用 stub 桩掉，同本文件既有
    # 那几条。**本地有 key 时不桩会真发请求**，那是这个仓库记了五次的环境泄漏。
    agent = _stub_agent(NarrationOutcome(text="尘埃落定。"))
    await agent.resolve_check(room_id, player_id, check_request_id, _capture, roll_value=7)

    assert notices, "装置自证：结算结果一次都没推出来"
    assert notices[0].rolled == 7, f"用的是 {notices[0].rolled}，不是玩家报的 7"
    # 成功等级仍然由后端按目标值判——那才是"规则权威在后端"
    assert notices[0].level, "成功等级没算"


async def test_without_a_reported_roll_the_server_still_rolls() -> None:
    """不报就是系统掷——**默认行为逐字不变**。

    只验"报了能用"的话，一个无条件读 `manual_roll` 的实现（None 时崩掉或恒为
    某个值）也可能溜过去。
    """
    room_id, player_id, nickname = await _seed_room()
    check_request_id = "chk-auto"
    await _enqueue(
        room_id,
        [
            _check(
                room_id=room_id,
                check_request_id=check_request_id,
                player_id=player_id,
                player_nickname=nickname,
                skill="侦察",
            )
        ],
    )
    notices: list = []

    async def _capture(notice) -> None:
        notices.append(notice)

    agent = _stub_agent(NarrationOutcome(text="尘埃落定。"))
    await agent.resolve_check(room_id, player_id, check_request_id, _capture)
    assert notices
    assert 1 <= notices[0].rolled <= 100


def test_the_payload_refuses_a_number_no_d100_can_roll() -> None:
    """🔴 「不能随便报」的第一道：报 0 或 101 不是作弊，是**这不是一颗 d100
    能掷出来的数**。范围校验在 DTO 层，够不到业务逻辑就先被挡掉。

    真作弊（报一个对自己有利的**合法**数）在「私有部署、自己和朋友玩」的定位
    下是社交问题不是技术问题——线下桌上报假数字比在软件里改数字容易得多。
    """
    import pydantic

    from app.dto.ws import CheckRollPayload

    assert CheckRollPayload.model_validate({"checkRequestId": "x", "rollValue": 1}).roll_value == 1
    assert CheckRollPayload.model_validate({"checkRequestId": "x", "rollValue": 100})
    # 不带 = 系统掷
    assert CheckRollPayload.model_validate({"checkRequestId": "x"}).roll_value is None
    for bad in (0, -1, 101, 999):
        with pytest.raises(pydantic.ValidationError):
            CheckRollPayload.model_validate({"checkRequestId": "x", "rollValue": bad})
