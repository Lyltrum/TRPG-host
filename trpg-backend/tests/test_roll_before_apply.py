"""掷骰那一步必须**零副作用**——`exec/34` 第 3 步的那条架构约束。

两段式玩家掷骰现在是「掷 → 广播 → 生效」三拍（此前掷与生效是同一拍）。
拆开是为了给幸运消费留位置（`exec/26 #66`）：它把失败推成成功，而玩家是
**看见骰子停下之后**才决定要不要花。副作用若留在掷骰那一步，花完幸运就得
逐个回滚记账/解隐匿/给叙事的文本——那是打地鼠，下一个副作用照样漏。

🔴 这条约束是**靠推理得出的作用域**，没有测试守着一定退化：下一个人往
`roll_check_only` 里塞一句 `record_event`，全套照样绿。所以这里断言的是
"掷完之后世界上什么都没变"，而不是某个函数的写法。

两片检定各验一遍：`san` 按规则不许花幸运，但它的**形状**必须一致，否则
又是那条「同一件事的两头，一头可插拔一头写死」。
"""

import random
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys, settle_hook_for
from app.core.keeper.capabilities.skill_check.attempts import CHECK_ATTEMPTS_KEY
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.pending import PendingDecision
from app.models.event import Event
from app.models.room import Character, Player, Room

_TESTS_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "trpg-backend") / "tests"
_MODULE = load_module(str(_TESTS_DIR / "fixtures" / "keeper_module.json"))

_db_path = Path(tempfile.mkdtemp(prefix="trpg-roll-apply-test-")) / "rollapply.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)

_STARTING_SAN = 50


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(room_code: str) -> tuple[str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="掷骰房",
            max_players=4,
            phase="InGame",
            keeper_state={},
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="凌铭辉")
        db.add(player)
        await db.flush()
        db.add(
            Character(
                room_id=room.id,
                player_id=player.id,
                status="complete",
                name="凌铭辉",
                occupation="记者",
                age=30,
                gender="男",
                attributes={
                    "STR": 60,
                    "CON": 70,
                    "SIZ": 50,
                    "DEX": 70,
                    "APP": 50,
                    "INT": 80,
                    "POW": 50,
                    "EDU": 70,
                    "LUCK": 55,
                },  # fmt: skip
                derived_stats={"HP": 12, "MP": 10, "SAN": _STARTING_SAN, "MOV": 8},
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
        rng=random.Random(7),
    )


async def _event_count(room_id: str) -> int:
    async with _session_factory() as db:
        return int(
            (
                await db.execute(select(func.count()).select_from(Event).filter_by(room_id=room_id))
            ).scalar_one()
        )


async def _san(room_id: str) -> int:
    async with _session_factory() as db:
        character = (await db.execute(select(Character).filter_by(room_id=room_id))).scalar_one()
        derived = character.derived_stats
        assert derived is not None
        return int(derived["SAN"])


def _skill_pending(room_id: str, player_id: str) -> PendingDecision:
    return PendingDecision.roll(
        decision_id="req-skill",
        kind="skill",
        room_id=room_id,
        player_id=player_id,
        player_nickname="凌铭辉",
        skill="侦察",
        loss_on_success="0",
        loss_on_failure="0",
        reason="翻找书桌",
        # 有地点，生效那一步才会真的往 keeper_state 记一笔——探针要有东西可看
        node="hall",
    )


def _san_pending(room_id: str, player_id: str) -> PendingDecision:
    return PendingDecision.roll(
        decision_id="req-san",
        kind="san",
        room_id=room_id,
        player_id=player_id,
        player_nickname="凌铭辉",
        skill=None,
        loss_on_success="1",
        loss_on_failure="1D6",
        reason="看见了那具尸体",
    )


async def _keeper_state(room_id: str) -> dict:
    """世界状态的探针。

    🔴 **它替下的是 `deps.check_results`**（2026-08-18 删）：那个字段是 07-23
    「掷骰可见性硬保证」的数据源，07-28 那个职责搬到结构化 WS 事件之后读取方
    被删、容器留了下来，于是这条用例有一半时间在守一个没人看的桶。
    换成 `keeper_state` 更准——技能检定的 `apply` 真的会往里写「本地检定次数」，
    那是**落库的副作用**，正是这条约束要防的东西。
    """
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        return dict(room.keeper_state or {})


async def test_rolling_a_skill_check_changes_nothing_until_apply() -> None:
    room_id, player_id = await _seed("RA100")
    deps = _deps(room_id, player_id)

    hook = settle_hook_for("skill")
    pending = _skill_pending(room_id, player_id)
    notice = await hook.run(deps, pending)

    assert notice.rolled > 0, "骰子该掷了——不然下面的断言只是在验一个没发生的动作"
    assert await _event_count(room_id) == 0, "🔴 掷骰那一步写了 events"
    assert await _keeper_state(room_id) == {}, "🔴 掷骰那一步写了 keeper_state"

    await hook.apply(deps, pending, notice)

    assert await _event_count(room_id) == 1
    # 生效那一步确实落了东西——没有这半句，把 apply() 改成空函数也照样绿
    assert CHECK_ATTEMPTS_KEY in await _keeper_state(room_id)


async def test_rolling_a_san_check_does_not_touch_the_character_until_apply() -> None:
    room_id, player_id = await _seed("RA200")
    deps = _deps(room_id, player_id)

    hook = settle_hook_for("san")
    pending = _san_pending(room_id, player_id)
    notice = await hook.run(deps, pending)

    assert notice.san_loss is not None and notice.san_loss > 0, (
        "这条用例需要一次真的有损失的理智检定"
    )
    assert await _san(room_id) == _STARTING_SAN, "🔴 掷骰那一步就把理智扣了"
    assert await _event_count(room_id) == 0, "🔴 掷骰那一步写了 events"

    await hook.apply(deps, pending, notice)

    assert await _san(room_id) == _STARTING_SAN - notice.san_loss
    assert await _event_count(room_id) == 1


async def test_every_settler_returns_something_appliable() -> None:
    """🔴 这是「逐个列出的地方」：加一种掷骰 kind 就要回来加一行。

    没有这条的话，第三种检定的 settler 完全可以照旧把副作用写在掷骰里——
    骨架拿到 `RolledCheck` 之后只调 `apply()`，不会有任何东西变红。
    """
    from app.core.keeper.runtime.pending import ROLL_KINDS

    assert frozenset({"skill", "san"}) == ROLL_KINDS, (
        "掷骰类多了一种：给它在本文件里补一条「掷完之前世界没变」的用例"
    )
