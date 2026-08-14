"""「调查员能力」局面块（2026-08-14 实测补的缺口）。

裁决器此前看不到任何角色能力数据，于是让话术 5 的调查员每一句问话都掷话术
（95% 必败），21 次检定里 10 次是同一个侦察，而玩家真正擅长的技能一次没用上。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.narration.party_sheet import format_party_sheet
from app.core.keeper.narration.prompts import format_turn_input
from app.core.keeper.narration.situation import build_situation
from app.models.room import Character, Player, Room

_RULESET = build_coc7_ruleset()
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "keeper_module.json"

_db_path = Path(tempfile.mkdtemp(prefix="trpg-party-sheet-")) / "t.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


def _character(**kw) -> Character:
    base = {
        "name": "凌铭辉",
        "occupation": "私家侦探",
        "attributes": {
            "STR": 60,
            "CON": 55,
            "SIZ": 65,
            "DEX": 65,
            "APP": 50,
            "INT": 70,
            "POW": 65,
            "EDU": 75,
            "LUCK": 45,
        },
        "derived_stats": {"HP": 8, "SAN": 65, "MP": 13},
        "skills": {"spot-hidden": 51, "fast-talk": 5},
    }
    base.update(kw)
    return Character(**base)


def test_only_skills_the_player_actually_put_points_into_are_listed() -> None:
    """全卡 80 多个技能，全量进 prompt 是纯噪音——只列高于初始值的那些。

    `spot-hidden` 初始 25 → 点到 51，进；`fast-talk` 初始 5 → 卡上也是 5，
    **不进**（没点过就等于初始值，模型按规则自己推得出来）。
    """
    text = format_party_sheet([("凌铭辉", _character())], _RULESET)
    assert "侦察(spot-hidden) 51" in text
    assert "fast-talk" not in text
    assert "私家侦探" in text


def test_attributes_and_current_vitals_are_always_given() -> None:
    text = format_party_sheet([("凌铭辉", _character())], _RULESET)
    assert "力量 60" in text
    assert "教育 75" in text
    assert "生命 8" in text  # 当前值，不是上限
    assert "理智 65" in text


def test_the_block_tells_the_model_what_the_unlisted_skills_mean() -> None:
    """🔴 光给一张表不够：模型得知道"没列出来的都是初始值、掷了大概率白掷"，
    否则它照样会对着一个 5% 的技能发检定（实测里 6 次话术全是这么来的）。"""
    text = format_party_sheet([("凌铭辉", _character())], _RULESET)
    assert "没列出的技能一律是初始值" in text
    assert "别掷" in text


def test_a_card_with_nothing_trained_says_so_instead_of_going_silent() -> None:
    """空列表要说出来。静默省略会让模型以为"这块没渲染"，而不是"这张卡很白"。"""
    text = format_party_sheet([("新手", _character(skills={}))], _RULESET)
    assert "没有点过任何技能" in text


def test_missing_ruleset_or_cards_renders_nothing() -> None:
    assert format_party_sheet([], _RULESET) == ""
    assert format_party_sheet([("凌铭辉", _character())], None) == ""


def test_missing_vitals_are_omitted_not_faked() -> None:
    """缺数据显式不写，**不编默认值**——静默兜底是明令禁止的那一族。"""
    text = format_party_sheet([("凌铭辉", _character(derived_stats={}))], _RULESET)
    assert "当前：" not in text
    assert "力量 60" in text  # 属性还在


def test_the_sheet_sits_before_every_capability_block() -> None:
    """🔴 顺序是有意义的：这张表回答"这些人有几斤几两"，是判断"要不要掷"的
    **前提**。摆在能力块后面就等于让模型先决定再看卡。"""
    text = format_turn_input(
        {"当前场景": "疗养院门廊"},
        ["玩家：我要跟他说明详细情况"],
        ["凌铭辉"],
        "凌铭辉",
        "我要跟他说明详细情况",
        capability_blocks=[(60.0, "## 密级配对状态\n（略）\n\n")],
        party_sheet=format_party_sheet([("凌铭辉", _character())], _RULESET),
    )
    assert text.index("调查员能力") < text.index("密级配对状态")
    assert text.index("在场调查员") < text.index("调查员能力")


@pytest.mark.parametrize("keeper_view", [True, False])
def test_the_sheet_is_keeper_only(keeper_view: bool) -> None:
    """叙事器不需要技能数值——给了反而诱它把数字写进散文。"""
    text = format_turn_input(
        None,
        [],
        ["凌铭辉"],
        "凌铭辉",
        "我要搜书房",
        party_sheet="侦察 51" if keeper_view else "",
    )
    assert ("侦察 51" in text) is keeper_view


@pytest.fixture
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.asyncio
async def test_build_situation_actually_fills_the_sheet(_fresh_db) -> None:
    """🔴 `party_sheet` 有默认空串，所以"忘了传"跟"这局没有卡"长得一模一样——
    两头都不会变红。这条专门钉住**组装器真的把它填进去了**。

    在此之前 `build_situation` 一条测试都没有，而它正是这类缺口最容易出现的地方。
    """
    module = load_module(_FIXTURE)
    async with _session_factory() as db:
        room = Room(room_code="PSH001", room_name="卡片房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="凌铭辉")
        db.add(player)
        await db.flush()
        db.add(_character(room_id=room.id, player_id=player.id))
        await db.commit()
        room_id, player_id = room.id, player.id

    builder = await build_situation(
        session_factory=_session_factory,
        module=module,
        room_id=room_id,
        observer_id=player_id,
        keeper_state={},
        history_lines=[],
        roster=["凌铭辉"],
        players=[(player_id, "凌铭辉")],
        phase=None,
        ending_id=None,
        is_heartbeat=False,
        is_opening_ceremony=False,
        ruleset=_RULESET,
    )
    assert "侦察(spot-hidden) 51" in builder.party_sheet

    # 而且它真的到了裁决那一拍的成品文本里（不是只躺在字段上）
    keeper_text = builder.for_keeper(nickname="凌铭辉", utterance="我要跟他说明详细情况")
    assert "调查员能力" in keeper_text
    assert "侦察(spot-hidden) 51" in keeper_text
    # 叙事那一拍拿不到
    narrator_text = builder.render(
        audience=None, ledger="", nickname="凌铭辉", utterance="我要跟他说明详细情况"
    )
    assert "侦察(spot-hidden) 51" not in narrator_text
