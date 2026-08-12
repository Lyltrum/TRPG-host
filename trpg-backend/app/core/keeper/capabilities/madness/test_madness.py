"""临时性疯狂：进入由代码强制，解除必须走裁决字段。

两条判据在这里各有断言：

- **地基 = 有 id 的东西。** 症状此前只是掷骰文本末尾的一句警告，由叙事器
  现编，下一轮没有任何地方记着它。
- **`#46`：没有 schema 字段的状态出不来。** 隐匿的解除当初只写在 prompt 里，
  于是隐匿永不解除。所以这里必须验"字段真的能解除"，而不是只验能进入。
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import COC7_MADNESS_SYMPTOMS, build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import field_capabilities, reserved_state_keys, situation_blocks
from app.core.keeper.capabilities.madness.schema import MadnessRecovery
from app.core.keeper.capabilities.san_check.executor import apply_san_check, san_check_impl
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.contract.registry import Capability
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.madness_state import MADNESS_KEY, load_madness, pick_symptom
from app.core.keeper.runtime.pending import PendingDecision
from app.core.keeper.runtime.turn_executor import execute_side_effects
from app.core.narration.contract import CheckResultNotice
from app.dto.game import MadnessSymptomSpec, RulesetRead
from app.models.room import Character, Player, Room

_MODULE = ScenarioModule.model_validate(
    {
        "meta": {"id": "madness-fixture", "title": "试验模组"},
        "kp_truth": {"summary": "地窖里的东西不该动。"},
        "player_intro": "你站在地窖门口。",
        "nodes": [{"id": "cellar", "title": "地窖", "kp_text": "它动了。"}],
    }
)


# ── 症状表本身 ────────────────────────────────────


def test_the_symptom_table_covers_the_whole_d10() -> None:
    """1D10 每一格都要有条目：缺格 = 掷到那个点数什么都不会发生（静默）。"""
    rolls = sorted(spec.roll for spec in COC7_MADNESS_SYMPTOMS)
    assert rolls == list(range(1, 11))


def test_symptom_ids_are_unique() -> None:
    ids = [spec.id for spec in COC7_MADNESS_SYMPTOMS]
    assert len(set(ids)) == len(ids)


def test_pick_reads_the_roll_column_not_the_list_index() -> None:
    """🔴 靠下标就等于默默假设"表一定按点数排好"。这里给一张**倒序**的表：
    掷到 1 必须拿到 roll=1 那条，而它排在列表最后。"""
    reversed_table = RulesetRead(
        attributes=[],
        skills=[],
        occupations=[],
        madness_symptoms=[
            MadnessSymptomSpec(id="second", roll=2, label="二", description="二"),
            MadnessSymptomSpec(id="first", roll=1, label="一", description="一"),
        ],
    )

    class _AlwaysOne(random.Random):
        def randint(self, a: int, b: int) -> int:  # noqa: ARG002
            return 1

    assert pick_symptom(reversed_table, _AlwaysOne()) is not None
    picked = pick_symptom(reversed_table, _AlwaysOne())
    assert picked is not None and picked.id == "first"


def test_a_ruleset_without_the_table_has_no_madness() -> None:
    """没有症状表 = 这套规则没有疯狂概念。不伪造一条默认症状。"""
    bare = RulesetRead(attributes=[], skills=[], occupations=[])
    assert pick_symptom(bare, random.Random(1)) is None


# ── 注册 ──────────────────────────────────────────


def test_the_state_key_is_reserved_and_the_field_is_permissioned() -> None:
    # 模型一条 state_updates 改不动它
    assert MADNESS_KEY in reserved_state_keys()
    # 单独一条权限（同 SET_HIDING）：玩家只是问了句话不该让人"康复"
    assert field_capabilities()["madness_recovered"] is Capability.CLEAR_MADNESS


def test_the_adjudicator_cannot_make_anyone_go_mad() -> None:
    """🔴 进入没有字段——触发条件是代码算出来的数，交回去让模型说就是重猜一遍。"""
    field_names = set(KeeperDecision.model_fields)
    assert "madness_recovered" in field_names
    others = [n for n in field_names if n.startswith("madness_") and n != "madness_recovered"]
    assert not others


# ── 局面块 ────────────────────────────────────────


def _mad(player_id: str, symptom_id: str) -> dict:
    return {MADNESS_KEY: f"{player_id}@{symptom_id}"}


def test_the_block_is_empty_when_nobody_is_mad() -> None:
    blocks = situation_blocks(_MODULE, {}, players=(("p1", "阿福"),), ruleset=build_coc7_ruleset())
    assert not any("疯狂中的调查员" in body for _order, body in blocks)


def test_the_block_names_the_symptom_and_demands_an_explicit_recovery() -> None:
    blocks = situation_blocks(
        _MODULE,
        _mad("p1", "paranoia"),
        players=(("p1", "阿福"),),
        ruleset=build_coc7_ruleset(),
    )
    body = "".join(b for _order, b in blocks if "疯狂中的调查员" in b)
    assert "阿福" in body and "偏执" in body
    # #46 的落点：局面块必须每轮都提醒"不写就一直疯着"
    assert "madness_recovered" in body


def test_a_symptom_id_that_no_longer_exists_is_not_printed_raw() -> None:
    """规则表换过之后那条记录查不到。**宁可整块不渲染也不打印裸 id**——模型
    读到没见过的英文 id 只会照字面现编一种发作表现。"""
    blocks = situation_blocks(
        _MODULE,
        _mad("p1", "no-such-symptom"),
        players=(("p1", "阿福"),),
        ruleset=build_coc7_ruleset(),
    )
    assert not any("no-such-symptom" in body for _order, body in blocks)


def test_without_a_ruleset_the_block_stays_silent() -> None:
    """`SituationContext.ruleset` 是可空的（既有能力不受影响）——拿不到规则
    数据时这块整块不渲染，而不是打印 id。"""
    blocks = situation_blocks(_MODULE, _mad("p1", "paranoia"), players=(("p1", "阿福"),))
    assert not any("疯狂中的调查员" in body for _order, body in blocks)


# ── 走真实执行链 ──────────────────────────────────

_db_path = Path(tempfile.mkdtemp(prefix="trpg-madness-test-")) / "madness.db"
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
async def solo() -> KeeperDeps:
    async with _session_factory() as db:
        room = Room(room_code="MAD001", room_name="疯房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="阿福", is_host=True)
        db.add(player)
        await db.flush()
        db.add(
            Character(
                room_id=room.id,
                player_id=player.id,
                status="complete",
                name="侦探福",
                occupation="私家侦探",
                age=32,
                gender="男",
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
                },
                derived_stats={"HP": 10, "MP": 10, "SAN": 70, "MOV": 8},
                skills={"spot-hidden": 70},
            )
        )
        await db.commit()
        room_id, player_id = room.id, player.id

    return KeeperDeps(
        room_id=room_id,
        player_id=player_id,
        session_factory=_session_factory,
        module=_MODULE,
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
        turn_player_ids=(player_id,),
        rng=random.Random(7),
    )


async def _keeper_state(deps: KeeperDeps) -> dict:
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        return dict(room.keeper_state or {})


async def test_a_big_single_loss_puts_him_in_madness_without_anyone_asking(solo) -> None:
    """🔴 代码强制：损失表达式两头都写死 6，成败都 ≥5 ⇒ 必然发作。

    没有任何一步经过模型——这正是"能确定性判断的一律代码强制"。
    """
    deps = solo
    await san_check_impl(deps, loss_on_success="6", loss_on_failure="6")
    madness = load_madness(await _keeper_state(deps))
    assert list(madness) == [deps.player_id]
    assert madness[deps.player_id] in {spec.id for spec in COC7_MADNESS_SYMPTOMS}


async def test_a_small_loss_does_not(solo) -> None:
    deps = solo
    await san_check_impl(deps, loss_on_success="4", loss_on_failure="4")
    assert load_madness(await _keeper_state(deps)) == {}


async def test_the_two_stage_path_also_triggers_it(solo) -> None:
    """🔴 真实主路是两段式（玩家点了按钮才掷），而它的 `detail` 是
    `apply_san_check` 现搭的、**没有 player_id**。第一版从 detail 里取
    player_id，单段路径能过、这条会 KeyError。"""
    deps = solo
    pending = PendingDecision.roll(
        kind="san",
        room_id=deps.room_id,
        player_id=deps.player_id,
        player_nickname="阿福",
        skill=None,
        loss_on_success="6",
        loss_on_failure="6",
        reason="它动了",
    )
    notice = CheckResultNotice(
        check_request_id=pending.decision_id,
        kind="san",
        player_id=deps.player_id,
        skill=None,
        rolled=90,
        target=70,
        level="失败",
        san_loss=6,
        san_remaining=64,
    )
    await apply_san_check(deps, pending, notice)
    assert list(load_madness(await _keeper_state(deps))) == [deps.player_id]


async def test_a_second_bout_does_not_silently_swap_the_symptom(solo) -> None:
    """已经在疯的人不再掷新症状：覆盖等于把局面块上那一行悄悄换掉。"""
    deps = solo
    await san_check_impl(deps, loss_on_success="6", loss_on_failure="6")
    first = load_madness(await _keeper_state(deps))[deps.player_id]
    await san_check_impl(deps, loss_on_success="6", loss_on_failure="6")
    assert load_madness(await _keeper_state(deps))[deps.player_id] == first


async def test_the_decision_field_is_what_lifts_it(solo) -> None:
    """🔴 `#46` 的正面用例：写了字段才解除。"""
    deps = solo
    await san_check_impl(deps, loss_on_success="6", loss_on_failure="6")
    assert load_madness(await _keeper_state(deps))

    recovery = MadnessRecovery(player="阿福", reason="被按住肩膀")
    reports, issues = await execute_side_effects(deps, KeeperDecision(madness_recovered=[recovery]))
    assert issues == []
    assert any("不再处于疯狂状态" in line for line in reports)
    assert load_madness(await _keeper_state(deps)) == {}


async def test_recovering_someone_who_is_not_mad_reports_nothing(solo) -> None:
    """「写了 ≠ 变了」：他本来就没在疯，执行报告里不许出现"他缓过来了"。"""
    deps = solo
    reports, issues = await execute_side_effects(
        deps, KeeperDecision(madness_recovered=[MadnessRecovery(player="阿福")])
    )
    assert reports == []
    assert issues and "并不在疯狂中" in issues[0]


async def test_an_unknown_name_becomes_an_issue_not_a_crash(solo) -> None:
    deps = solo
    reports, issues = await execute_side_effects(
        deps, KeeperDecision(madness_recovered=[MadnessRecovery(player="查无此人")])
    )
    assert reports == []
    assert issues
