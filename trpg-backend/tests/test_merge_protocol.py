"""分组变更协议：分开乐观执行、会合必须确认（`exec/33 §5`）。

## 这条测试守的是什么

分头此前是**概率性**的：「谁跟谁在一处」由裁决器每轮重写的位置派生，于是每轮
都有一次写错分组的机会。2026-08-10 多人实测实证：它把 `current_node_id` 与
`moves` 写矛盾，被明确留下的队友被拖进地下室 → 系统认为没分头 → 全房间广播是
**完全正确的执行**，只是建立在错的位置上。**保证等于最弱的那一环。**

协议是**不对称**的，因为两个方向的错误代价不同：

- **分开**判错 → 多隔离一个人：困惑、可恢复、不泄露 → 乐观执行。
- **会合**判错 → 两组信息合并：**泄露、不可撤回** → 必须由当事人确认。
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
from app.core.keeper.capabilities import reserved_state_keys
from app.core.keeper.capabilities.movement.schema import PlayerMove
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.location_state import (
    confirm_merge_impl,
    group_players,
    is_party_split,
)
from app.core.keeper.runtime.pending import MERGE_CONFIRM_KIND, pending_decision_manager
from app.core.keeper.runtime.turn_executor import execute_side_effects
from app.models.room import Character, Player, Room

_FIXTURE_MODULE = Path(__file__).parent / "fixtures" / "keeper_module.json"

_db_path = Path(tempfile.mkdtemp(prefix="trpg-merge-")) / "merge.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _character(room_id: str, player_id: str, name: str) -> Character:
    return Character(
        room_id=room_id,
        player_id=player_id,
        status="complete",
        name=name,
        occupation="记者",
        age=30,
        gender="男",
        attributes={
            "STR": 50,
            "CON": 50,
            "SIZ": 50,
            "DEX": 50,
            "APP": 50,
            "INT": 60,
            "POW": 50,
            "EDU": 60,
            "LUCK": 50,
        },
        derived_stats={"HP": 10, "MP": 10, "SAN": 50, "MOV": 8},
        skills={"spot-hidden": 50},
    )


@pytest.fixture
async def party() -> tuple[KeeperDeps, str, str]:
    """阿福（本轮发言者）+ 阿贵，都是真人。"""
    async with _session_factory() as db:
        room = Room(room_code="MRG001", room_name="会合房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        a = Player(room_id=room.id, nickname="阿福", is_host=True)
        b = Player(room_id=room.id, nickname="阿贵")
        db.add_all([a, b])
        await db.flush()
        db.add_all([_character(room.id, a.id, "阿福"), _character(room.id, b.id, "阿贵")])
        await db.commit()
        room_id, a_id, b_id = room.id, a.id, b.id

    deps = KeeperDeps(
        room_id=room_id,
        player_id=a_id,
        session_factory=_session_factory,
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
        turn_player_ids=(a_id,),
        rng=random.Random(1),
    )
    return deps, a_id, b_id


async def _merge_pending(room_id: str) -> set[str]:
    """谁正挂着「你跟他们碰上了吗」。

    🔴 `exec/34` 之后这件事的真相在**待决定队列**里，不再是 `keeper_state`
    的一个自由键——待掷检定早就有的落库/重连补发，会合确认从此共用同一套。
    """
    async with _session_factory() as db:
        return await pending_decision_manager.player_ids_of_kind(db, room_id, MERGE_CONFIRM_KIND)


async def _state(deps: KeeperDeps) -> dict:
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        return dict(room.keeper_state or {})


async def _split(deps: KeeperDeps) -> None:
    """把两个人分到 hall / cellar。"""
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="cellar")])
    )


# ── 分开：乐观执行，不打断 ──────────────────────────


async def test_splitting_apart_needs_no_confirmation(party) -> None:
    """分开是安全方向：立刻生效，不挂确认卡。"""
    deps, a_id, b_id = party
    await _split(deps)
    state = await _state(deps)
    assert await _merge_pending(deps.room_id) == set()
    assert is_party_split(state, [a_id, b_id]) is True


async def test_moving_to_an_empty_place_is_not_a_merge(party) -> None:
    """目的地没人 = 不是会合，一样不挂卡。"""
    deps, _a_id, _b_id = party
    await _split(deps)
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="hidden-safe")])
    )
    assert await _merge_pending(deps.room_id) == set()


# ── 会合：挂起，等当事人确认 ────────────────────────


async def test_walking_into_someone_holds_them_apart_until_confirmed(party) -> None:
    """🔴 走到别人所在的地点 → 位置照写，但**投递上仍然分开**，直到本人确认。"""
    deps, a_id, b_id = party
    await _split(deps)
    # 阿贵从地窖走回门厅——那里有阿福
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="hall")])
    )
    state = await _state(deps)
    assert await _merge_pending(deps.room_id) == {b_id}
    # 位置确实写了（位置是唯一地基，不新增第二份真相）
    assert state["玩家位置"].count("hall") == 2
    # 但投递上还是两组——判错的方向必须朝保密
    pending = await _merge_pending(deps.room_id)
    grouped = group_players(state, [a_id, b_id], pending)
    assert [members for _loc, members in grouped] == [[a_id], [b_id]]
    # 🔴 必须把待确认集合传进去才看得见这次分头：默认空集会让它答"没分头"——
    # 那正是 `group_players` 不给默认值的理由（漏传朝泄露方向失败）。
    assert is_party_split(state, [a_id, b_id], pending) is True


async def test_walking_back_yourself_needs_no_confirmation(party) -> None:
    """🔴 收窄（2026-08-11，用户真机反馈"手动汇合很奇怪"）：**他自己说要过去的
    就别再问一遍**。

    两条判据都由代码判：① 他被 `moves` 逐人点名挪动；② 他本轮自己发过言。
    这里阿贵自己发言、自己被点名走回门厅 —— 那就是他的意思。
    """
    deps, a_id, b_id = party
    await _split(deps)
    deps.turn_player_ids = (b_id,)
    deps.player_id = b_id
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="hall")])
    )
    assert await _merge_pending(deps.room_id) == set()
    state = await _state(deps)
    assert is_party_split(state, [a_id, b_id]) is False


async def test_being_summoned_by_someone_else_still_needs_confirmation(party) -> None:
    """对照：他这一轮**一个字都没说**却被挪过去 —— 那是别人替他做的决定，非问不可。

    `test_walking_into_someone_holds_them_apart_until_confirmed` 已经是这个形状
    （fixture 的发言者是阿福），这里显式把"谁发言"写出来，免得收窄条件哪天被
    改成只看"有没有被点名"而没有东西变红。
    """
    deps, _a_id, b_id = party
    await _split(deps)
    deps.turn_player_ids = (_a_id,)  # 只有阿福发言
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="hall")])
    )
    assert await _merge_pending(deps.room_id) == {b_id}


async def test_confirming_merges_them(party) -> None:
    deps, a_id, b_id = party
    await _split(deps)
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="hall")])
    )
    async with _session_factory() as db:
        assert await confirm_merge_impl(db, deps.room_id, b_id) is True
    state = await _state(deps)
    assert await _merge_pending(deps.room_id) == set()
    assert is_party_split(state, [a_id, b_id]) is False


async def test_confirming_twice_is_a_no_op(party) -> None:
    """幂等：重复点确认不报错，也不改任何东西。"""
    deps, _a_id, b_id = party
    await _split(deps)
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="hall")])
    )
    async with _session_factory() as db:
        assert await confirm_merge_impl(db, deps.room_id, b_id) is True
    async with _session_factory() as db:
        assert await confirm_merge_impl(db, deps.room_id, b_id) is False


async def test_leaving_again_cancels_the_card(party) -> None:
    """人又走了 → 那张确认卡作废（`exec/33 §5.3`）。

    不清的话他会**永远单独一组**：「没确认就维持分离」是对的默认，但已经离开
    那个地点之后还挂着，就变成永久隔离了。
    """
    deps, _a_id, b_id = party
    await _split(deps)
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="hall")])
    )
    assert await _merge_pending(deps.room_id) == {b_id}
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="cellar")])
    )
    assert await _merge_pending(deps.room_id) == set()


async def test_the_card_survives_the_whole_group_changing_scene(party) -> None:
    """🔴 全组一起换个地方 → 人还在一起 → 那张卡**不作废**（2026-08-10 验证跑）。

    第一版把"还算不算数"写成 `pending[player] != node_id`——待确认记录里存了
    一份位置拷贝，位置一变就对不上，于是卡被当成过期丢掉，**没人点头就合并了**。
    作废的条件是"身边一个人都没有"，不是"位置变了"。
    """
    deps, a_id, b_id = party
    await _split(deps)
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="hall")])
    )
    assert await _merge_pending(deps.room_id) == {b_id}
    # 两个人一起挪到暗格（谁都没走散）
    await execute_side_effects(deps, KeeperDecision(current_node_id="hidden-safe"))
    state = await _state(deps)
    assert await _merge_pending(deps.room_id) == {b_id}, "🔴 没确认就被合并了"
    grouped = group_players(state, [a_id, b_id], await _merge_pending(deps.room_id))
    assert [members for _loc, members in grouped] == [[a_id], [b_id]]


async def test_the_key_is_reserved_from_state_updates(party) -> None:
    """记账键是代码写的，`state_updates` 碰不到。"""
    assert MERGE_CONFIRM_KIND not in reserved_state_keys(), (
        "🔴 它不再是 keeper_state 的键了（exec/34）——真相在待决定队列里，留一份镜像就是两份真相"
    )


# ── 退化保证 ────────────────────────────────────────


async def test_a_party_that_never_splits_never_sees_the_protocol(party) -> None:
    """全队一直在一起 → 协议一次都不触发，行为与改动前逐字一致。"""
    deps, a_id, b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    await execute_side_effects(deps, KeeperDecision(current_node_id="cellar"))
    state = await _state(deps)
    assert await _merge_pending(deps.room_id) == set()
    assert is_party_split(state, [a_id, b_id]) is False
