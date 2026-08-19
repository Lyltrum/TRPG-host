"""established 能力的验收：既成事实只增不减，且不跟别的账本重复。

它跟 `open_threads` 的分界是**生命周期**，不是重要程度——那条分界就是这一片
存在的全部理由，所以用例主要盯它。
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
from app.core.keeper.capabilities.established.executor import execute_established
from app.core.keeper.capabilities.established.schema import (
    EstablishedDecisionFields,
    NewFact,
)
from app.core.keeper.capabilities.established.state import (
    DUPLICATE_RATIO,
    ESTABLISHED_KEY,
    ESTABLISHED_SEQ_KEY,
    SUSPECT_RATIO,
    format_established,
    load_established,
    next_fact_id,
)
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.contract.registry import SituationContext, TurnFacts
from app.core.keeper.runtime.deps import KeeperDeps
from app.models.room import Player, Room

_TESTS_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "trpg-backend") / "tests"
_MODULE = load_module(_TESTS_DIR / "fixtures" / "keeper_module.json")

_db_path = Path(tempfile.mkdtemp(prefix="trpg-established-test-")) / "keeper.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def deps() -> KeeperDeps:
    async with _session_factory() as db:
        room = Room(room_code="ESTA01", room_name="定局房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="阿福")
        db.add(player)
        await db.commit()
        room_id, player_id = room.id, player.id
    return KeeperDeps(
        room_id=room_id,
        player_id=player_id,
        session_factory=_session_factory,
        module=_MODULE,
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
        rng=random.Random(1),
    )


async def _state(deps: KeeperDeps) -> dict:
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        return dict(room.keeper_state or {})


def _decision(*texts: str) -> EstablishedDecisionFields:
    return EstablishedDecisionFields(new_facts=[NewFact(text=t) for t in texts])


# ── 记下来 ───────────────────────────────────────


async def test_a_fact_is_recorded_with_an_id(deps: KeeperDeps) -> None:
    report, issues = await execute_established(
        deps, _decision("调查员烧掉了林中的木屋"), TurnFacts()
    )
    assert issues == []
    assert "烧掉了林中的木屋" in report[0]

    table = load_established(await _state(deps))
    assert list(table) == ["fact-1"]
    assert table["fact-1"]["text"] == "调查员烧掉了林中的木屋"


async def test_ids_only_go_up(deps: KeeperDeps) -> None:
    """🔴 只增不复用，理由同 open_threads：复盘里两件事共用一个 id 就分不开。

    这一片的条目不会被删，但"从表里现算最大号"本身是错的形状——照抄正确的那个。
    """
    await execute_established(deps, _decision("木屋烧毁了"), TurnFacts())
    await execute_established(deps, _decision("管家死了"), TurnFacts())
    state = await _state(deps)
    assert sorted(load_established(state)) == ["fact-1", "fact-2"]
    assert state[ESTABLISHED_SEQ_KEY] == 2


async def test_the_sequence_survives_even_if_the_table_is_wiped(deps: KeeperDeps) -> None:
    """序号存在 state 里，不从表里现算——否则表被清过之后 id 会回填。"""
    await execute_established(deps, _decision("木屋烧毁了"), TurnFacts())
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        room.keeper_state = {**(room.keeper_state or {}), ESTABLISHED_KEY: {}}
        await db.commit()

    await execute_established(deps, _decision("另一件事"), TurnFacts())
    assert list(load_established(await _state(deps))) == ["fact-2"]


async def test_blank_text_is_dropped_not_stored_as_an_empty_row(deps: KeeperDeps) -> None:
    await execute_established(deps, _decision("   "), TurnFacts())
    assert load_established(await _state(deps)) == {}


async def test_nothing_written_when_the_decision_is_empty(deps: KeeperDeps) -> None:
    """退化保证：没写 new_facts 的轮次，keeper_state 一个字都不该多。"""
    report, issues = await execute_established(deps, _decision(), TurnFacts())
    assert (report, issues) == ([], [])
    assert await _state(deps) == {}


# ── 🔴 生命周期：没有结清动作 ──────────────────────


def test_there_is_no_way_to_resolve_a_fact() -> None:
    """🔴 这一片跟 `open_threads` 的**唯一**分界，也是它存在的全部理由。

    悬而未决有 `resolved_threads`（执行时是 `pop`，条目真的消失）；既成事实
    **不许有**对应动作——「烧掉的木屋」被标成已解决，那条记忆当场蒸发，而
    十几小时的局里那正是最典型的失忆。

    变异检验入口：给 schema 加一个 `resolved_facts` 字段，这条会红。
    """
    fields = set(EstablishedDecisionFields.model_fields)
    assert fields == {"new_facts"}


# ── 局面块 ───────────────────────────────────────


def _context(state: dict | None) -> SituationContext:
    return SituationContext(module=_MODULE, keeper_state=state)


def test_empty_table_renders_nothing() -> None:
    """退化证明：没记过就整块不渲染，局面块与加这一片之前逐字一致。"""
    assert format_established(_context(None)) == ""
    assert format_established(_context({})) == ""


def test_every_fact_is_listed(deps: KeeperDeps) -> None:
    """🔴 全量列出，不许"只显示最近 N 条"。

    这一片的**全部价值**就是"十几小时之后还记得"——裁剪展示等于把它做成了
    一个更慢的 L3。
    """
    state = {ESTABLISHED_KEY: {f"fact-{i}": {"text": f"第 {i} 件定局"} for i in range(1, 30)}}
    text = format_established(_context(state))
    for i in range(1, 30):
        assert f"第 {i} 件定局" in text


def test_malformed_rows_are_dropped_whole(deps: KeeperDeps) -> None:
    """形状不对的条目整条丢弃，不产生半条记录。"""
    state = {
        ESTABLISHED_KEY: {"fact-1": "不是字典", "fact-2": {"text": ""}, "fact-3": {"text": "好的"}}
    }
    assert list(load_established(state)) == ["fact-3"]


def test_next_id_is_pure() -> None:
    assert next_fact_id(0) == ("fact-1", 1)
    assert next_fact_id(7) == ("fact-8", 8)


def test_the_block_shows_ids_so_the_model_can_tell_what_already_exists() -> None:
    """🔴 局面块必须带 `fact-N`（2026-08-16 真机调出来的）。

    第一版只渲染文本，实测同一件事被记了两遍——`fact-1 点燃了地下室的煤油`
    与 `fact-2 点燃了地下室，火势已起`，分别来自相邻的两拍。

    悬而未决那一片天然没这个毛病：它**必须**显示 `thread-N` 供
    `resolved_threads` 引用，**id 于是顺带承担了"这条已经有了"的信号**。
    这一片没有结清动作，所以 id 的唯一作用就是这个——但它确实是必要的。
    """
    state = {ESTABLISHED_KEY: {"fact-1": {"text": "木屋烧毁了"}}}
    assert "fact-1" in format_established(_context(state))


def test_facts_are_listed_in_numeric_order() -> None:
    """按 `fact-N` 的数字排，不是按字符串——否则 fact-10 会排在 fact-2 前面，
    而这一块是给模型读的"已经有哪些"，顺序乱了它更容易重复记。"""
    state = {
        ESTABLISHED_KEY: {
            "fact-10": {"text": "第十件"},
            "fact-2": {"text": "第二件"},
            "fact-1": {"text": "第一件"},
        }
    }
    text = format_established(_context(state))
    assert text.index("第一件") < text.index("第二件") < text.index("第十件")


# ── 2026-08-18 双人真机：id 不够，去重要代码做 ──────────────────


#: 真机那两条。同一拍的**两次裁决**各记了一条，而第二次裁决看得见 `fact-2`
#: （`_load_room_memory` 每次开新 session，不是脏读）——⇒ 把已有的摆出来
#: 只是必要条件。
_THE_REAL_DUPLICATE = (
    "程雨眠用撬棍砸碎了朱印船深处房间的渡轮模型",
    "程雨眠砸碎了朱印船深处房间的渡轮模型",
)


async def test_the_second_wording_of_the_same_event_is_not_recorded(
    deps: KeeperDeps,
) -> None:
    """🔴 **这条是这次改动的全部理由**（2026-08-18 双人真机原样复现）。

    **变异检验**：把 `executor.py` 里那句 `if same_as is not None: continue`
    删掉，这条当场红。
    """
    first, second = _THE_REAL_DUPLICATE
    await execute_established(deps, _decision(first), TurnFacts())
    report, issues = await execute_established(deps, _decision(second), TurnFacts())

    assert report == [], "重复的那条不该进执行报告"
    assert any("重复" in issue and "fact-1" in issue for issue in issues), issues
    assert list(load_established(await _state(deps))) == ["fact-1"]


async def test_a_duplicate_does_not_count_as_the_world_moving_forward(
    deps: KeeperDeps,
) -> None:
    """🔴 A3：`world_advanced` 是「无进展轮数」的清零条件之一 ⇒ 重复记账会把
    打转计数按住不动（双人真机 5 拍打同一场僵局，那个数只涨到 2）。

    **变异检验**：把 `if report:` 改回 `if new_facts:`，这条当场红。
    """
    first, second = _THE_REAL_DUPLICATE

    fresh = TurnFacts()
    await execute_established(deps, _decision(first), fresh)
    assert fresh.world_advanced_this_turn is True, "前提：新的一条确实算推进"

    dup = TurnFacts()
    await execute_established(deps, _decision(second), dup)
    assert dup.world_advanced_this_turn is False
    assert dup.world_advanced_by == []


async def test_a_genuinely_different_fact_still_gets_through(deps: KeeperDeps) -> None:
    """🔴 **纯否定的收窄会压死整片能力**：这一片的全部价值是"十几小时后还记得"，
    误删一条真事实的代价高于漏拦一条重复。

    这条文本跟上一条**同句式、只换了主语与宾语**（实测相似度 0.647，正好夹在
    两个真重复样本 0.923 与 0.600 之间）——`DUPLICATE_RATIO` 取 0.85 就是为了
    让它过得去。
    """
    await execute_established(deps, _decision(_THE_REAL_DUPLICATE[1]), TurnFacts())
    report, issues = await execute_established(
        deps, _decision("霍启元砸碎了朱印船深处房间的神像"), TurnFacts()
    )
    assert report, "这是另一件事，必须记下来"
    assert list(load_established(await _state(deps))) == ["fact-1", "fact-2"]
    # 判不准的那一档：照常记，但要留一条供事后看
    assert any("疑似重复" in issue for issue in issues), issues


def test_the_two_thresholds_bracket_the_real_samples() -> None:
    """🔴 **阈值是拿真样本标定的，这条把标定结果钉住**。

    三个数一变，上面三条端到端就会开始飘——把它们直接写成断言，改阈值的人
    当场看得见自己踩到了哪个样本。
    """
    import difflib

    def ratio(a: str, b: str) -> float:
        return difflib.SequenceMatcher(None, a, b).ratio()

    # 真重复 A（双人局，同一拍两次裁决）
    assert ratio(*_THE_REAL_DUPLICATE) >= DUPLICATE_RATIO
    # 不该判重复的（同句式换主宾）——必须落在拦截线之下
    assert (
        SUSPECT_RATIO
        <= ratio(
            "程雨眠砸碎了朱印船深处房间的渡轮模型",
            "霍启元砸碎了朱印船深处房间的神像",
        )
        < DUPLICATE_RATIO
    )
    # 真重复 B（08-16 煤油）：**拦不住**，只落在"报而不断"那一档。
    # 这不是缺陷是取舍——它跟上面那条负样本没法用一个阈值分开。
    assert SUSPECT_RATIO <= ratio("点燃了地下室的煤油", "点燃了地下室，火势已起") < DUPLICATE_RATIO
