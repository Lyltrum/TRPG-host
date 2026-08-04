"""开放收尾的模组：`endings[]` 合法为空（`exec/29`）。

## 为什么放开

组装层原先被两处一起顶着「必须有结局」：阶段 1 的 prompt 规则 C，和校验器那道
「有结局信号片段 → endings[] 不得为空」的门。实测林中屋的 `endings[0]` 因此是
**伪造**的——源头是原文一行「模组尾声，提供战役延续的可能性」。

伪造的代价不在组装那一步，在下游：收束的唯一入口是 `ending_reached`，而那条假
结局没有任何玩家行动能触发它，于是**这个模组结构上永远收束不了**，试跑只会报
「没走到结局」并把账算在模组头上。

## 🔴 放开之后活过来的那个静默兜底

原判断是 `if module.endings and not any(e.id == eid for e in module.endings)`。
空列表让前半段短路成 False，**于是走进 else，任何字符串都能把对局判成 finished**。
六个预设模组个个非空，所以这个分支一直够不着——放开为空的同一刻它就活了。

fixture 沿用原创迷你庄园失窃案，与任何第三方模组原文无关。
"""

from __future__ import annotations

import json
import random
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys
from app.core.keeper.capabilities.progression.executor import execute_progression
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import (
    NO_ENDINGS_NOTICE,
    ScenarioModule,
    load_module,
    render_endings,
    render_full,
)
from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.phase import PHASE_FINISHED, load_phase
from app.models.room import Player, Room

_FIXTURE_MODULE = Path(__file__).parent / "fixtures" / "keeper_module.json"

_db_path = Path(tempfile.mkdtemp(prefix="trpg-open-ended-test-")) / "open.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


def _module(*, endings: bool) -> ScenarioModule:
    raw = json.loads(_FIXTURE_MODULE.read_text(encoding="utf-8"))
    if not endings:
        raw["endings"] = []
    return ScenarioModule.model_validate(raw)


async def _deps(module: ScenarioModule) -> KeeperDeps:
    async with _session_factory() as db:
        room = Room(room_code="OPEN01", room_name="开放收尾房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="阿福", is_host=True)
        db.add(player)
        await db.commit()
        room_id, player_id = room.id, player.id

    return KeeperDeps(
        room_id=room_id,
        player_id=player_id,
        session_factory=_session_factory,
        module=module,
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
        turn_player_ids=(player_id,),
        rng=random.Random(7),
    )


async def _phase(deps: KeeperDeps) -> str | None:
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        return load_phase(dict(room.keeper_state or {}))


# ── 收束闸门 ──────────────────────────────────────────


async def test_module_without_endings_cannot_be_finished() -> None:
    """🔴 没有结局的模组收束不了——而且要**说出来**，不是静默放行。

    变异检验：把执行层的 `if not deps.module.endings:` 那一支删掉，这条立刻红
    （对局被判成 finished）。
    """
    deps = await _deps(_module(endings=False))

    report, issues = await execute_progression(
        deps, KeeperDecision(ending_reached="随便编一个"), TurnFacts()
    )

    assert await _phase(deps) != PHASE_FINISHED
    assert report == []
    assert len(issues) == 1 and "没有预设结局" in issues[0]


async def test_known_ending_id_still_finishes() -> None:
    """必然通过那一头：模组有结局、id 对得上，照常收束。"""
    module = _module(endings=True)
    assert module.endings, "fixture 该有结局，否则这条用例什么都没验"
    deps = await _deps(module)

    report, issues = await execute_progression(
        deps, KeeperDecision(ending_reached=module.endings[0].id), TurnFacts()
    )

    assert await _phase(deps) == PHASE_FINISHED
    assert issues == [] and report


async def test_unknown_ending_id_is_still_rejected() -> None:
    """两条拒绝理由要分得开——「没有结局」和「id 不认识」是不同的毛病。"""
    deps = await _deps(_module(endings=True))

    _report, issues = await execute_progression(
        deps, KeeperDecision(ending_reached="没有这条"), TurnFacts()
    )

    assert len(issues) == 1 and "剧本里没有 ending id" in issues[0]


# ── 说给守秘人听 ──────────────────────────────────────


def test_empty_endings_render_as_an_explicit_notice() -> None:
    """🔴 空段落是最坏的表达：标题在、底下什么都没有，模型只能自己猜。"""
    assert render_endings(_module(endings=False)) == NO_ENDINGS_NOTICE
    assert NO_ENDINGS_NOTICE in render_full(_module(endings=False))


def test_notice_tells_the_keeper_what_to_do_not_just_what_is_missing() -> None:
    """光说"没有结局"不够——裁决 prompt 还写着每轮判 trigger，得给它一个动作。"""
    assert "ending_reached" in NO_ENDINGS_NOTICE


def test_modules_with_endings_are_untouched() -> None:
    """既有六个模组个个非空，渲染必须逐字不变——不然磁带全要重录。"""
    module = load_module(_FIXTURE_MODULE)
    rendered = render_endings(module)

    assert NO_ENDINGS_NOTICE not in rendered
    assert all(e.id in rendered for e in module.endings)
