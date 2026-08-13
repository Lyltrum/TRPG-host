"""closure：没有预设结局时的自然收尾（`exec/30 §10.4`）。

守两件事：
1. 「还剩多少内容」三个计数的口径——含**缺数据显式降级**（不许报 0）与
   **扁平遍历**（只数顶层节点会得出假数字）；
2. 反向门只禁危险方向——还在揭线索 / 还有一次性议程没触发时不许收尾。
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
from app.core.keeper.capabilities.closure.executor import execute_closure
from app.core.keeper.capabilities.closure.remaining import (
    format_remaining_content,
    unfired_agenda_count,
    unrevealed_pair_count,
    unvisited_node_count,
)
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.location_state import PLAYER_LOCATION_KEY
from app.core.keeper.runtime.phase import (
    PHASE_ENDING,
    PHASE_FINISHED,
    PHASE_KEY,
    load_phase,
)
from app.core.keeper.runtime.progress_state import (
    AGENDA_FIRED_KEY,
    CLUES_REVEALED_KEY,
    STALLED_TURNS_KEY,
    VISITED_NODES_KEY,
    load_stalled_turns,
    load_visited_nodes,
)
from app.models.room import Character, Player, Room

_TESTS_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "trpg-backend") / "tests"
_MODULE = load_module(str(_TESTS_DIR / "fixtures" / "keeper_module.json"))

#: fixture 模组扁平展开是 4 个节点：hall / hall-footprints / cellar，
#: 外加挂在 `sub_node`（**单数**）下的 hidden-safe。
#: 🔴 写这条用例时我的探查脚本只遍历了 `sub_nodes`（复数），当场少数一个——
#: 树形结构有两个子节点字段，这就是"报数量前先确认口径"的现场。
_ALL_NODES = 4
_ONCE_AGENDA = "night-1-footprints"
_PAIRS = 2

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-closure-test-")) / "closure.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(room_code: str, state: dict) -> tuple[str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="收尾房",
            max_players=4,
            phase="InGame",
            keeper_state=state,
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
                    "STR": 50,
                    "CON": 50,
                    "SIZ": 50,
                    "DEX": 50,
                    "APP": 50,
                    "INT": 50,
                    "POW": 50,
                    "EDU": 50,
                    "LUCK": 50,
                },  # fmt: skip
                derived_stats={"HP": 10, "MP": 10, "SAN": 50, "MOV": 8},
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
        rng=random.Random(0),
    )


async def _phase_of(room_id: str) -> str | None:
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        return load_phase(room.keeper_state)


async def _state_of(room_id: str) -> dict:
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        return dict(room.keeper_state or {})


async def _write_state(room_id: str, patch: dict) -> None:
    """就地改几个键（模拟别的能力在这一轮之前写过东西）。"""
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        room.keeper_state = {**dict(room.keeper_state or {}), **patch}
        await db.commit()


# ── 计数口径 ─────────────────────────────────────────


def test_node_count_walks_the_whole_tree_not_just_the_top_level() -> None:
    """🔴 只数顶层会得出"这个模组只有 2 个 node"——我已经凭这种假数字下过大结论。"""
    assert unvisited_node_count(_MODULE, {}) == _ALL_NODES
    assert unvisited_node_count(_MODULE, {VISITED_NODES_KEY: "hall, hall-footprints"}) == 2


def test_only_once_agenda_counts_as_remaining_content() -> None:
    """`once=False` 的事件可以反复发生，"还没发生过"对它不成立——拿它当
    "内容没跑完"的证据会让对局**永远收不了尾**。"""
    assert unfired_agenda_count(_MODULE, {}) == 1  # 两条议程里只有一条是 once
    assert unfired_agenda_count(_MODULE, {AGENDA_FIRED_KEY: _ONCE_AGENDA}) == 0


def test_pair_count_reads_the_room_wide_ledger() -> None:
    assert unrevealed_pair_count(_MODULE, {}) == _PAIRS
    assert unrevealed_pair_count(_MODULE, {CLUES_REVEALED_KEY: "pair-hall-mud@*"}) == _PAIRS - 1


def test_missing_data_degrades_explicitly_instead_of_reporting_zero() -> None:
    """🔴 导入的模组曾经根本不产 `visibility_pairs`，「未揭开」于是恒为 0——
    而 0 跟"全揭开了"长得一模一样，正好把收尾门推向放行。"""
    bare = _MODULE.model_copy(update={"visibility_pairs": [], "agenda": [], "nodes": []})
    assert unrevealed_pair_count(bare, {}) is None
    assert unfired_agenda_count(bare, {}) is None
    assert unvisited_node_count(bare, {}) is None
    # 🔴 行为变更（2026-08-12）：三样存量全缺时**不再整块省略**——停滞轮数跟
    # 模组有没有数据无关，而"什么都数不出来的模组"恰恰是最需要它的那一种。
    bare_text = format_remaining_content(bare, {})
    assert "没有配对数据" in bare_text
    assert "没有新进展" in bare_text
    # 只缺一样时那一行要说清楚缺了什么，不能写成 0
    no_pairs = _MODULE.model_copy(update={"visibility_pairs": []})
    text = format_remaining_content(no_pairs, {})
    assert "没有配对数据" in text
    assert "还剩 0" not in text


# ── 去过的节点 ───────────────────────────────────────


async def test_visits_are_recorded_from_the_location_table_each_turn() -> None:
    """🔴 回合级读位置表，不挂在每个写位置的函数上：位置有三条写入路径
    （`current_node_id` / `moves` / 即兴地点），逐个挂就会漏。"""
    room_id, player_id = await _seed("CLS100", {PLAYER_LOCATION_KEY: "p1@hall, p2@cellar"})
    report, issues = await execute_closure(_deps(room_id, player_id), KeeperDecision(), TurnFacts())
    assert issues == []
    assert any("hall" in line and "cellar" in line for line in report)
    assert set(load_visited_nodes(await _state_of(room_id))) == {"hall", "cellar"}


async def test_recording_the_same_place_twice_is_a_no_op() -> None:
    """幂等：没新去过的地方就不写库、也不给叙事塞一句废话。"""
    room_id, player_id = await _seed(
        "CLS200", {PLAYER_LOCATION_KEY: "p1@hall", VISITED_NODES_KEY: "hall"}
    )
    report, issues = await execute_closure(_deps(room_id, player_id), KeeperDecision(), TurnFacts())
    assert issues == []
    assert report == []


# ── 无进展轮数 ───────────────────────────────────────


async def test_stalled_turns_count_up_when_nothing_new_happens() -> None:
    """🔴 前三份记账全是存量（还剩多少），回答不了「是不是在原地打转」——
    而真人 KP 判断收尾时数的正是后者。玩家可以一直偏离主线，存量永远不见底。"""
    room_id, player_id = await _seed(
        "CLS800", {PLAYER_LOCATION_KEY: "p1@hall", VISITED_NODES_KEY: "hall"}
    )
    for expected in (1, 2, 3):
        await execute_closure(_deps(room_id, player_id), KeeperDecision(), TurnFacts())
        assert load_stalled_turns(await _state_of(room_id)) == expected


async def test_going_somewhere_new_resets_the_stall_counter() -> None:
    room_id, player_id = await _seed(
        "CLS810",
        {PLAYER_LOCATION_KEY: "p1@hall", VISITED_NODES_KEY: "hall", STALLED_TURNS_KEY: "4"},
    )
    await execute_closure(_deps(room_id, player_id), KeeperDecision(), TurnFacts())
    assert load_stalled_turns(await _state_of(room_id)) == 5

    await _write_state(room_id, {PLAYER_LOCATION_KEY: "p1@cellar"})
    await execute_closure(_deps(room_id, player_id), KeeperDecision(), TurnFacts())
    assert load_stalled_turns(await _state_of(room_id)) == 0


async def test_revealing_a_clue_also_counts_as_progress() -> None:
    """进展的口径只有代码确定性可判的两样：去了新地方、揭开了新线索。"""
    room_id, player_id = await _seed(
        "CLS820",
        {PLAYER_LOCATION_KEY: "p1@hall", VISITED_NODES_KEY: "hall", STALLED_TURNS_KEY: "6"},
    )
    facts = TurnFacts(clues_revealed_this_turn=True)
    await execute_closure(_deps(room_id, player_id), KeeperDecision(), facts)
    assert load_stalled_turns(await _state_of(room_id)) == 0


# ── 反向门 ───────────────────────────────────────────


def _done_state(**extra: str) -> dict:
    """内容跑完的状态：配对全揭、一次性议程已触发、三个节点都去过。"""
    return {
        PLAYER_LOCATION_KEY: "p1@cellar",
        CLUES_REVEALED_KEY: "pair-butler-faces@*, pair-hall-mud@*",
        AGENDA_FIRED_KEY: _ONCE_AGENDA,
        VISITED_NODES_KEY: "hall, hall-footprints, cellar, hidden-safe",
        **extra,
    }


async def test_content_exhausted_closes_the_story_without_inventing_an_ending_id() -> None:
    """🔴 行为变更（2026-08-12）：终点是 `ending` 而不是 `finished`。

    自然收尾纯属 KP 判断，所以给它一个**可撤回**的中间态；直达 `finished`
    的只有命中剧本预设结局那条路。
    """
    room_id, player_id = await _seed("CLS300", _done_state())
    report, issues = await execute_closure(
        _deps(room_id, player_id), KeeperDecision(story_ran_its_course=True), TurnFacts()
    )
    assert issues == []
    assert any("进入收尾" in line for line in report)
    assert await _phase_of(room_id) == PHASE_ENDING
    # 🔴 不许凭空造一个剧本里不存在的结局 id
    from app.core.keeper.runtime.phase import ENDING_ID_KEY

    assert (await _state_of(room_id)).get(ENDING_ID_KEY) in (None, "")


async def test_a_turn_that_just_revealed_a_clue_may_not_close() -> None:
    """还在往外掏线索的那一轮，故事显然还在往下走。"""
    room_id, player_id = await _seed("CLS400", _done_state())
    facts = TurnFacts(clues_revealed_this_turn=True)
    _report, issues = await execute_closure(
        _deps(room_id, player_id), KeeperDecision(story_ran_its_course=True), facts
    )
    assert any("刚揭开新线索" in i for i in issues)
    assert await _phase_of(room_id) != PHASE_FINISHED


async def test_an_untriggered_once_agenda_no_longer_blocks_the_close() -> None:
    """🔴 行为变更（2026-08-12）：议程没跑完**不再拦**。

    真人 KP 完全可以在议程没跑完时收尾——那条门同样是代码替他做决定。它现在
    降级成局面块里的一个数（参考材料）。反向门只剩「本轮刚揭开新线索」那条
    自相矛盾的。
    """
    room_id, player_id = await _seed("CLS500", _done_state(**{AGENDA_FIRED_KEY: ""}))
    _report, issues = await execute_closure(
        _deps(room_id, player_id), KeeperDecision(story_ran_its_course=True), TurnFacts()
    )
    assert not any("议程" in i for i in issues)
    assert await _phase_of(room_id) == PHASE_ENDING


async def test_a_scripted_ending_this_turn_wins_and_is_not_closed_twice() -> None:
    """progression（order=80）先跑。它已经按剧本结局收束过了就不重复收——
    否则剧本自己的落幕会被一个无 ending_id 的收尾覆盖掉。"""
    room_id, player_id = await _seed("CLS600", _done_state(**{PHASE_KEY: PHASE_FINISHED}))
    report, issues = await execute_closure(
        _deps(room_id, player_id), KeeperDecision(story_ran_its_course=True), TurnFacts()
    )
    assert issues == []
    assert not any("自然收尾" in line for line in report)


async def test_not_writing_the_field_never_closes_anything() -> None:
    """退化保证：字段是 false 时这片能力只记账，不碰阶段。"""
    room_id, player_id = await _seed("CLS700", _done_state())
    _report, issues = await execute_closure(
        _deps(room_id, player_id), KeeperDecision(), TurnFacts()
    )
    assert issues == []
    assert await _phase_of(room_id) != PHASE_FINISHED
