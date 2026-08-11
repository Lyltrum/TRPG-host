"""不替未发言的玩家行动（exec/19 #41）。

真人实测 2026-07-31：只有凌铭辉提交了行动，叙事却写出「张家豪扫了一眼鞋柜旁
那双沾泥的雨靴」——张家豪那一轮什么都没输出。叙事 prompt 里本来就有一句
「不替玩家决定下一步」，但它**没有名单**；代码明明知道本轮谁发了言。

这里验证两件事：
1. `build_bystander_hint` 的措辞与空名单退化；
2. `_narrate_per_audience` 把名单**按每段的受众**算出来交给叙事阶段——
   分头时别组的人名一个字都不能出现在本段提示里（否则投递做的隔离会被
   这条 prompt 自己泄回去）。

不验证模型会不会听话：那是概率性的，同 `NO_PENDING_CHECK_HINT`。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.narration.narration_hints import build_bystander_hint, build_person_hint
from app.core.keeper.runtime.agent import KeeperAgent
from app.core.keeper.runtime.location_state import PLAYER_LOCATION_KEY
from app.core.keeper.runtime.phase import PHASE_INVESTIGATION, PHASE_KEY
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY
from app.core.narration.contract import NarrationContext, PlayerUtterance
from app.models.room import Player, Room

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-bystander-test-")) / "bystander.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ── 1. 纯函数 ───────────────────────────────────────


def test_no_bystanders_adds_nothing() -> None:
    assert build_bystander_hint([]) == ""


def test_single_bystander_is_named() -> None:
    hint = build_bystander_hint(["张家豪"])
    assert "张家豪" in hint
    assert "这一轮什么都没说" in hint
    assert "不得替他写出" in hint


def test_multiple_bystanders_are_all_named() -> None:
    hint = build_bystander_hint(["张家豪", "阿贵"])
    assert "张家豪、阿贵" in hint
    assert "不得替他们写出" in hint


# ── 2. 接线：按受众算名单 ────────────────────────────


def _keeper() -> KeeperAgent:
    return KeeperAgent(
        api_key="fake-key",
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
    )


async def _seed(room_code: str, keeper_state: dict) -> tuple[str, str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="旁观者房",
            max_players=4,
            phase="InGame",
            keeper_state={PHASE_KEY: PHASE_INVESTIGATION, **keeper_state},
        )
        db.add(room)
        await db.flush()
        a = Player(room_id=room.id, nickname="凌铭辉")
        b = Player(room_id=room.id, nickname="张家豪")
        db.add_all([a, b])
        await db.flush()
        await db.commit()
        return room.id, a.id, b.id


def _stub(agent: KeeperAgent) -> list[str]:
    suffixes: list[str] = []

    async def fake_adjudicate(situation: str) -> KeeperDecision:
        return KeeperDecision(thinking="无事", narration_guidance="继续")

    async def fake_narrate_prose(
        situation,
        decision,
        report,
        issues,
        *,
        max_tokens,
        max_chars,
        extra_suffix="",
        tape_key=None,
    ):
        suffixes.append(extra_suffix)
        return f"第{len(suffixes)}段叙事。"

    agent._adjudicate = fake_adjudicate  # ty: ignore[invalid-assignment]
    agent._narrate_prose = fake_narrate_prose  # ty: ignore[invalid-assignment]
    return suffixes


async def test_silent_teammate_is_named_in_hint() -> None:
    """两人同处一地，本轮只有凌铭辉发言 → 提示里点名张家豪。"""
    agent = _keeper()
    suffixes = _stub(agent)
    room_id, a_id, _b_id = await _seed("BYS001", {CURRENT_NODE_KEY: "hall"})

    await agent.narrate(
        NarrationContext(
            utterance="我推门进去",
            player_nickname="凌铭辉",
            room_id=room_id,
            player_id=a_id,
        )
    )
    assert len(suffixes) == 1
    assert "张家豪" in suffixes[0]
    assert "这一轮什么都没说" in suffixes[0]


async def test_all_speakers_get_no_bystander_hint() -> None:
    """两人都提交了行动 → 没有旁观者，不追加这段提示。"""
    agent = _keeper()
    suffixes = _stub(agent)
    room_id, a_id, b_id = await _seed("BYS002", {CURRENT_NODE_KEY: "hall"})

    await agent.narrate(
        NarrationContext(
            utterance="凌铭辉：我推门\n张家豪：我跟上",
            player_nickname="凌铭辉",
            room_id=room_id,
            player_id=a_id,
            participant_ids=(a_id, b_id),
            utterances=(
                PlayerUtterance(player_id=a_id, nickname="凌铭辉", text="我推门"),
                PlayerUtterance(player_id=b_id, nickname="张家豪", text="我跟上"),
            ),
        )
    )
    assert len(suffixes) == 1
    assert "这一轮什么都没说" not in suffixes[0]


async def test_split_party_hint_never_names_the_other_group() -> None:
    """🔴 分头时，另一处那个人**不在这段的受众里**——他的名字一个字都不能出现。

    他不是"本轮没发言的旁观者"，他压根不在场。把他写进提示等于用 prompt
    把 per-observer 投递刚隔离掉的信息泄回去。
    """
    agent = _keeper()
    suffixes = _stub(agent)
    room_id, a_id, b_id = await _seed("BYS003", {CURRENT_NODE_KEY: "hall"})
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        room.keeper_state = {**(room.keeper_state or {}), PLAYER_LOCATION_KEY: f"{b_id}@cellar"}
        await db.commit()

    await agent.narrate(
        NarrationContext(
            utterance="我看看四周",
            player_nickname="凌铭辉",
            room_id=room_id,
            player_id=a_id,
        )
    )
    assert len(suffixes) == 1
    assert "张家豪" not in suffixes[0]
    assert "这一轮什么都没说" not in suffixes[0]


# ── 人称按受众（exec/33 §10 #80）────────────────────


def test_person_hint_is_second_person_for_an_audience_of_one() -> None:
    """🔴 双人真机：叙事整个掉进全景第三人称小说体，而 prompt 一个字都没规定人称。

    单人局模型自己会用「你」；多人局「你」指代不明，它就选了网文的默认视角，
    顺带写起 PC 的内心与注意力。修法按受众定：这一段只发给一个人 ⇒ 用「你」。
    """
    hint = build_person_hint(["阿福"])
    assert "阿福" in hint and "第二人称" in hint
    # 内心戏是这条的另一半：GM 不替 PC 决定想什么、感觉什么
    assert "内心" in hint


def test_person_hint_uses_names_when_more_than_one_will_read_it() -> None:
    hint = build_person_hint(["阿福", "阿贵"])
    assert "阿福" in hint and "阿贵" in hint
    assert "第二人称" not in hint
    assert "内心" in hint


def test_person_hint_degrades_to_nothing() -> None:
    """没有受众 = 不加这一段（同 `build_bystander_hint` 的空名单退化）。"""
    assert build_person_hint([]) == ""


async def test_split_segment_gets_the_second_person_hint() -> None:
    """分头那一段的受众只有一个人 ⇒ 走「你」那一支，且不带别组的名字。"""
    agent = _keeper()
    suffixes = _stub(agent)
    room_id, a_id, b_id = await _seed("BYS004", {CURRENT_NODE_KEY: "hall"})
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        room.keeper_state = {**(room.keeper_state or {}), PLAYER_LOCATION_KEY: f"{b_id}@cellar"}
        await db.commit()

    await agent.narrate(
        NarrationContext(
            utterance="我看看四周",
            player_nickname="凌铭辉",
            room_id=room_id,
            player_id=a_id,
        )
    )
    assert len(suffixes) == 1
    assert "第二人称" in suffixes[0]
    assert "凌铭辉" in suffixes[0] and "张家豪" not in suffixes[0]
