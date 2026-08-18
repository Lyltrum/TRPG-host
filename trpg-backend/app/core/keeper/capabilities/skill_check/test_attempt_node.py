"""记账键钉在发起那一刻的地点上（2026-08-18 真机）。

## 🔴 记账键在一拍之内会漂

真机第 17 拍：玩家抓住绑匪的胳膊往回拽，这一拍连着掷了**两次力量**——同一个
对手、同一次拉扯。记账表里却是：

    talk-to-the-musician|力量: {"n": 1}
    funeral-scene|力量:         {"n": 1}

中间那次结算叙事改了 `当前场景节点`，而 `_tally_attempt` 读的是结算那一刻的
值。于是「这一处掷过几次这个技能」这个数**连同一拍都保不住**，而
`format_attempts` 的两道判据（少于 2 次不提、只列当前这一处）全都架在它上面。

修法跟 `reveals` 是同一个先例、同一个理由：**待掷期间场景会变**，所以要在
创建待掷记录时就把地点绑上去。
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
from app.core.keeper.capabilities.skill_check.attempts import CHECK_ATTEMPTS_KEY, load_attempts
from app.core.keeper.capabilities.skill_check.executor import (
    apply_skill_check,
    settle_skill_check,
)
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.pending import PendingDecision
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY
from app.models.room import Character, Player, Room

_TESTS_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "trpg-backend") / "tests"
_MODULE = load_module(str(_TESTS_DIR / "fixtures" / "keeper_module.json"))

_db_path = Path(tempfile.mkdtemp(prefix="trpg-attempt-node-test-")) / "attempts.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(room_code: str, *, standing_at: str) -> tuple[str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="记账房",
            max_players=4,
            phase="InGame",
            keeper_state={CURRENT_NODE_KEY: standing_at},
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="周砚")
        db.add(player)
        await db.flush()
        db.add(
            Character(
                room_id=room.id,
                player_id=player.id,
                status="complete",
                name="周砚",
                occupation="医生",
                age=23,
                gender="男",
                attributes={
                    "STR": 60,
                    "CON": 60,
                    "SIZ": 50,
                    "DEX": 60,
                    "APP": 50,
                    "INT": 70,
                    "POW": 60,
                    "EDU": 70,
                    "LUCK": 55,
                },  # fmt: skip
                derived_stats={"HP": 12, "MP": 12, "SAN": 60, "MOV": 8},
                skills={},
            )
        )
        await db.commit()
        return room.id, player.id


def _deps(room_id: str, player_id: str) -> KeeperDeps:
    return KeeperDeps(
        room_id=room_id,
        player_id=player_id,
        session_factory=_session_factory,
        module=_MODULE,
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
        rng=random.Random(1),
    )


def _pending(room_id: str, player_id: str, *, node: str | None) -> PendingDecision:
    return PendingDecision.roll(
        kind="skill",
        room_id=room_id,
        player_id=player_id,
        player_nickname="周砚",
        skill="力量",
        reason="把他从人质身上扯下来",
        node=node,
    )


async def _move_the_world_to(room_id: str, node_id: str) -> None:
    """模拟结算叙事把 `当前场景节点` 改掉——真机上就是这么发生的。"""
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        room.keeper_state = {**(room.keeper_state or {}), CURRENT_NODE_KEY: node_id}
        await db.commit()


async def _attempts(room_id: str) -> dict[str, dict[str, int]]:
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        return load_attempts(room.keeper_state)


async def test_the_tally_lands_where_the_roll_started_not_where_it_settled() -> None:
    """🔴 **变异检验**：把 `_tally_attempt` 里的 `pending.node or` 去掉，
    只读 `state.get(CURRENT_NODE_KEY)`，这条当场红——记账会落在 `funeral-scene`。
    """
    room_id, player_id = await _seed("TLY100", standing_at="talk-to-the-musician")
    deps = _deps(room_id, player_id)
    pending = _pending(room_id, player_id, node="talk-to-the-musician")

    # 待掷期间世界挪了——这正是真机那一拍发生的事
    await _move_the_world_to(room_id, "funeral-scene")

    notice = await settle_skill_check(deps, pending)
    await apply_skill_check(deps, pending, notice)

    table = await _attempts(room_id)
    assert "talk-to-the-musician|力量" in table
    assert "funeral-scene|力量" not in table


async def test_two_rolls_in_one_beat_stay_in_one_bucket() -> None:
    """真机那一拍的原样复现：同一次拉扯的两次力量必须落进同一格。"""
    room_id, player_id = await _seed("TLY101", standing_at="talk-to-the-musician")
    deps = _deps(room_id, player_id)

    for _ in range(2):
        pending = _pending(room_id, player_id, node="talk-to-the-musician")
        notice = await settle_skill_check(deps, pending)
        await apply_skill_check(deps, pending, notice)
        # 每次结算叙事都可能改指针
        await _move_the_world_to(room_id, "funeral-scene")

    table = await _attempts(room_id)
    assert table["talk-to-the-musician|力量"]["n"] == 2
    assert len([k for k in table if k.endswith("|力量")]) == 1


async def test_a_roll_queued_before_this_field_existed_still_gets_tallied() -> None:
    """显式降级：升级那一刻已经在队列里的记录没有 `node`，退回结算时的位置。

    精度差一点，但整条记账不会断掉——比静默丢弃一笔好。
    """
    room_id, player_id = await _seed("TLY102", standing_at="talk-to-the-musician")
    deps = _deps(room_id, player_id)
    pending = _pending(room_id, player_id, node=None)

    await _move_the_world_to(room_id, "funeral-scene")

    notice = await settle_skill_check(deps, pending)
    await apply_skill_check(deps, pending, notice)

    assert "funeral-scene|力量" in await _attempts(room_id)


async def test_nothing_is_recorded_when_the_player_is_off_the_map() -> None:
    """人不在任何节点上时没有"这一处"可挂——不硬编一个。"""
    room_id, player_id = await _seed("TLY103", standing_at="talk-to-the-musician")
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        room.keeper_state = {}
        await db.commit()

    deps = _deps(room_id, player_id)
    pending = _pending(room_id, player_id, node=None)
    notice = await settle_skill_check(deps, pending)
    await apply_skill_check(deps, pending, notice)

    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        assert CHECK_ATTEMPTS_KEY not in (room.keeper_state or {})
