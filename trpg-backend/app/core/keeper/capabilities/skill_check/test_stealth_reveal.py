"""潜行对抗失败 → 代码强制解除隐匿（exec/19 #46 的另一半）。

隐匿解除本来有三个触发点：**离开地点**（早就是代码硬化的）、**主动现身**、
**被发现**。后两个一直只写在裁决规则 4c 里请模型自觉写回 `hidden: false`，
于是真人实测里"一旦潜行就永远单独成段"——模型忘了写，隐匿就再也不会解除。

`exec/20 §2.7` 给的硬化方向是：「被发现」可以挂在**潜行对抗检定**失败上。
对抗检定的表达（`CheckRequest.opposed`）已经在 #38 里做完了，这里只是把
结算结果接到隐匿状态上——**判定权从模型手里拿走**。

只硬化"隐匿者本人掷潜行对抗"这一种写法，理由见 `settle_skill_check` 里那段
注释（反过来写时对手侧只有一个自由文本名字）。另一种写法仍是概率的，已在
`exec/20 §2.7` 如实登记。
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
from app.core.keeper.capabilities.skill_check.executor import (
    apply_skill_check,
    settle_skill_check,
)
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.location_state import HIDDEN_PLAYERS_KEY, load_hidden_players
from app.core.keeper.runtime.pending import PendingDecision
from app.core.narration.contract import CheckResultNotice
from app.models.room import Character, Player, Room

_TESTS_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "trpg-backend") / "tests"
_FIXTURE_MODULE = str(_TESTS_DIR / "fixtures" / "keeper_module.json")
_MODULE = load_module(_FIXTURE_MODULE)

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-stealth-test-")) / "stealth.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(room_code: str, *, hidden: bool) -> tuple[str, str]:
    """一个正在隐匿（或没在隐匿）的调查员。潜行没点过 → 基础值 20。"""
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="潜行房",
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
                derived_stats={"HP": 12, "MP": 10, "SAN": 50, "MOV": 8},
                skills={},
            )
        )
        if hidden:
            room.keeper_state = {HIDDEN_PLAYERS_KEY: player.id}
        await db.commit()
        return room.id, player.id


def _deps(room_id: str, player_id: str, seed: int) -> KeeperDeps:
    return KeeperDeps(
        room_id=room_id,
        player_id=player_id,
        session_factory=_session_factory,
        module=_MODULE,
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
        rng=random.Random(seed),
    )


def _pending(
    room_id: str, player_id: str, *, skill: str, opposed_value: int | None
) -> PendingDecision:
    return PendingDecision.roll(
        decision_id="req-1",
        kind="skill",
        room_id=room_id,
        player_id=player_id,
        player_nickname="凌铭辉",
        skill=skill,
        loss_on_success="0",
        loss_on_failure="0",
        reason="躲在屏风后面",
        opposed_opponent="科比特" if opposed_value is not None else None,
        opposed_value=opposed_value,
    )


async def _settle(deps: KeeperDeps, pending: PendingDecision) -> CheckResultNotice:
    """掷骰 + 生效。两步之间在生产路径上隔着一次广播（exec/34 第 3 步），
    这里不关心那一拍，只要"两步都走完"之后的结果。"""
    notice = await settle_skill_check(deps, pending)
    await apply_skill_check(deps, pending, notice)
    return notice


async def _hidden_ids(room_id: str) -> set[str]:
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        return load_hidden_players(room.keeper_state)


#: 掷骰是服务端权威的，所以胜负只能靠种子摆。每条用例都**在断言里再确认一次
#: 前提**（`opposed_won is False/True`）——换了 rng 实现时它会红，而不是悄悄
#: 变成"在验另一件事"。
_LOSING_SEED = 1  # 潜行 20 vs 90 → 输
_WINNING_SEED = 2  # 潜行 20 vs 5 → 赢
_CON_LOSING_SEED = 4  # 体质 70 vs 90 → 输


async def test_losing_a_stealth_contest_reveals_the_player() -> None:
    room_id, player_id = await _seed("STL100", hidden=True)
    deps = _deps(room_id, player_id, seed=_LOSING_SEED)

    notice = await _settle(deps, _pending(room_id, player_id, skill="潜行", opposed_value=90))

    assert notice.opposed_won is False, "种子选错了：这条用例需要主动方输"
    assert await _hidden_ids(room_id) == set(), "🔴 潜行对抗输了，人还挂在隐匿里"
    # 叙事那一拍必须知道他暴露了，否则代码改了状态、故事里他还藏着
    assert any("被发现" in line for line in deps.check_results)


async def test_winning_a_stealth_contest_keeps_the_player_hidden() -> None:
    room_id, player_id = await _seed("STL200", hidden=True)
    deps = _deps(room_id, player_id, seed=_WINNING_SEED)

    notice = await _settle(deps, _pending(room_id, player_id, skill="潜行", opposed_value=5))

    assert notice.opposed_won is True, "种子选错了：这条用例需要主动方赢"
    assert await _hidden_ids(room_id) == {player_id}


async def test_a_plain_stealth_check_does_not_reveal_anyone() -> None:
    """没有对手就没有"被谁发现"。普通潜行检定失败只是没藏好，不是暴露——
    真正把他置入隐匿的是裁决器的 `hiding`，这条路径不该反过来撤销它。"""
    room_id, player_id = await _seed("STL300", hidden=True)
    deps = _deps(room_id, player_id, seed=_LOSING_SEED)

    await _settle(deps, _pending(room_id, player_id, skill="潜行", opposed_value=None))

    assert await _hidden_ids(room_id) == {player_id}


async def test_losing_a_non_stealth_contest_does_not_reveal_anyone() -> None:
    """体质对抗毒烟输了不等于被发现——退化保证，别让所有对抗检定都掀开隐匿。"""
    room_id, player_id = await _seed("STL400", hidden=True)
    deps = _deps(room_id, player_id, seed=_CON_LOSING_SEED)

    notice = await _settle(deps, _pending(room_id, player_id, skill="体质", opposed_value=90))

    assert notice.opposed_won is False
    assert await _hidden_ids(room_id) == {player_id}


async def test_a_player_who_was_not_hiding_produces_no_noise() -> None:
    """没在隐匿的人潜行输了：不写 events、也不给叙事塞一句"不再隐匿"。

    「设了但什么都没发生」和「没设」看起来一样——所以这里断言的是**没有那句
    话**，而不是"函数返回了 False"。
    """
    room_id, player_id = await _seed("STL500", hidden=False)
    deps = _deps(room_id, player_id, seed=_LOSING_SEED)

    await _settle(deps, _pending(room_id, player_id, skill="潜行", opposed_value=90))

    assert await _hidden_ids(room_id) == set()
    assert not any("被发现" in line for line in deps.check_results)
