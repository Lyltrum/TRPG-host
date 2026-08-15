"""inventory：随身物品的增减（`exec/38`，2026-08-14 实测）。

起因是叙事写「州警扣下扳机」而玩家的随身是空的——那一枪从头到尾没有出处。
查下来整条链只差一半：建卡第 6 步早就有「装备与物品」那一栏，`sheet_digest`
也早就把「随身：…」渲进了裁决局面块，**唯独缺"剧情里拿到的东西怎么进来"**。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys
from app.core.keeper.capabilities.inventory.schema import EquipmentChange
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.turn_executor import execute_side_effects
from app.models.room import Character, Player, Room

_TESTS_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "trpg-backend") / "tests"
_MODULE = load_module(str(_TESTS_DIR / "fixtures" / "keeper_module.json"))

_db_path = Path(tempfile.mkdtemp(prefix="trpg-inventory-test-")) / "inv.db"
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
        room = Room(room_code="INV001", room_name="物品房", max_players=4, phase="InGame")
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
                name="阿福",
                occupation="记者",
                age=30,
                attributes={
                    "STR": 55,
                    "CON": 55,
                    "SIZ": 55,
                    "DEX": 55,
                    "APP": 55,
                    "INT": 55,
                    "POW": 55,
                    "EDU": 55,
                    "LUCK": 55,
                },  # fmt: skip
                derived_stats={"HP": 11, "MP": 11, "SAN": 55, "MOV": 8},
                skills={},
                equipment=["手电筒", "笔记本"],
            )
        )
        await db.commit()
        return KeeperDeps(
            room_id=room.id,
            player_id=player.id,
            session_factory=_session_factory,
            module=_MODULE,
            ruleset=build_coc7_ruleset(),
            reserved_state_keys=reserved_state_keys(),
        )


async def _items(deps: KeeperDeps) -> list[str]:
    async with _session_factory() as db:
        from sqlalchemy import select

        character = (
            await db.scalars(select(Character).where(Character.room_id == deps.room_id))
        ).one()
        return list(character.equipment or [])


def _change(**kw) -> KeeperDecision:  # noqa: ANN003
    return KeeperDecision(equipment_changes=[EquipmentChange(**kw)])


# ── 拿到 / 失去 ─────────────────────────────────────


async def test_gaining_an_item_adds_it(deps: KeeperDeps) -> None:
    report, issues = await execute_side_effects(deps, _change(gained=["撬棍"], reason="工具棚"))

    assert await _items(deps) == ["手电筒", "笔记本", "撬棍"]
    assert any("撬棍" in line for line in report)
    assert issues == []


async def test_losing_an_item_removes_it(deps: KeeperDeps) -> None:
    _report, issues = await execute_side_effects(deps, _change(lost=["手电筒"], reason="掉进洞里"))

    assert await _items(deps) == ["笔记本"]
    assert issues == []


async def test_the_players_own_wording_is_preserved(deps: KeeperDeps) -> None:
    """🔴 增量而不是快照：随身清单是**玩家自己写的东西**。

    每轮让模型重报全量，它迟早会把「祖父留下的怀表」改写成「怀表」，或者漏掉
    一件。`cast` 能用快照是因为那张表本来就是模型每轮重算的，里面没有玩家写的字。
    """
    await execute_side_effects(deps, _change(gained=["祖父留下的怀表"]))
    await execute_side_effects(deps, _change(gained=["绳子"]))

    assert await _items(deps) == ["手电筒", "笔记本", "祖父留下的怀表", "绳子"]


# ── 拿不到的东西不许悄悄消失 ───────────────────────


async def test_losing_something_he_never_had_changes_nothing(deps: KeeperDeps) -> None:
    """🔴 名字对不上就**什么都不删**并说出来。

    删错的代价是玩家的东西凭空没了，而那是他自己写下的字。
    """
    before = await _items(deps)
    _report, issues = await execute_side_effects(deps, _change(lost=["猎枪"]))

    assert await _items(deps) == before
    assert any("身上没有" in issue for issue in issues), issues
    # 拒绝要配一条走得通的修法
    assert any("一字不差" in issue for issue in issues), issues


async def test_only_exact_names_match(deps: KeeperDeps) -> None:
    """🔴 对照组：不做近义匹配。

    「手电」不是「手电筒」——判断它们是不是同一件是语义判断，模糊匹配是同义词
    打地鼠的开始。没有这一条，把比较改成"包含"也会绿，而那会删错东西。
    """
    _report, issues = await execute_side_effects(deps, _change(lost=["手电"]))

    assert "手电筒" in await _items(deps)
    assert issues


async def test_case_and_whitespace_still_match(deps: KeeperDeps) -> None:
    """抄写抖动不该让东西删不掉——只归一空白与大小写，不做别的。"""
    await execute_side_effects(deps, _change(gained=["Zippo 打火机"]))
    _report, issues = await execute_side_effects(deps, _change(lost=[" zippo 打火机 "]))

    assert "Zippo 打火机" not in await _items(deps)
    assert issues == []


async def test_gaining_a_duplicate_is_refused(deps: KeeperDeps) -> None:
    """已经有的东西不再加一遍——否则连点两轮就有两个手电筒。"""
    _report, issues = await execute_side_effects(deps, _change(gained=["手电筒"]))

    assert await _items(deps) == ["手电筒", "笔记本"]
    assert any("已经有" in issue for issue in issues), issues


# ── 退化保证 ────────────────────────────────────


async def test_a_turn_without_changes_touches_nothing(deps: KeeperDeps) -> None:
    before = await _items(deps)
    report, issues = await execute_side_effects(deps, KeeperDecision())

    assert await _items(deps) == before
    assert not any("拿到" in line or "失去" in line for line in report)
    assert issues == []
