"""迷茫/怪话/明确行动分类：从正则匹配改用裁决 LLM 的 player_state 字段
（exec/13，对应 `docs/keeper-design/exec/09-真人实测问题清单.md` #8）。

根因不是"正则不够全"——`prose_discipline.py` 三组正则要求关键词字面
严格相邻（如 `r"我该(怎么|做|干)"` 要求"我"和"该"紧挨着），真人实测
"我现在该做什么"（中间插了"现在"）就匹配不上，这是正则做语义分类的
结构性上限。改法是把分类判断交给已经在读这句话的裁决 LLM，在
`KeeperDecision` 里加 `player_state` 字段；只有裁决完全失败（走
`_FALLBACK_ADJUDICATE_GUIDANCE` 兜底）时才退回正则作为安全网。

不跑真实 LLM——`_adjudicate`/`_narrate_prose` 用实例属性桩掉，只验证
`agent.py::narrate()` 里"三个布尔值从哪来"这段路由逻辑。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.agent import _FALLBACK_ADJUDICATE_GUIDANCE, KeeperAgent
from app.core.keeper.decision import CheckRequest, KeeperDecision
from app.core.keeper.module_loader import load_module
from app.core.keeper.phase import PHASE_INVESTIGATION, PHASE_KEY
from app.core.keeper.prose_discipline import is_player_confused
from app.core.narrator import NarrationContext
from app.models.room import Player, Room

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-player-state-test-")) / "agent.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _keeper() -> KeeperAgent:
    return KeeperAgent(
        api_key="fake-key",
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
    )


async def _seed_room(room_code: str, nickname: str = "阿福") -> tuple[str, str]:
    """建一个已处于调查阶段（跳过开场仪式分支）的房间 + 一名玩家。"""
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="分类测试房",
            max_players=4,
            phase="InGame",
            keeper_state={PHASE_KEY: PHASE_INVESTIGATION},
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname=nickname)
        db.add(player)
        await db.flush()
        await db.commit()
        return room.id, player.id


def _stub_agent(agent: KeeperAgent, decision: KeeperDecision) -> dict:
    """桩掉 `_adjudicate`/`_narrate_prose`，返回一个 dict——narrate() 结束后
    `captured["decision"]` 就是最终喂给叙事阶段的（已被代码强制注入过的）
    decision，供测试断言 guidance/checks 是否被改动。"""
    captured: dict = {}

    async def fake_adjudicate(situation: str) -> KeeperDecision:
        return decision

    async def fake_narrate_prose(situation, decision, report, issues, *, max_tokens, max_chars):
        captured["decision"] = decision
        return "占位叙事文本。"

    agent._adjudicate = fake_adjudicate  # ty: ignore[invalid-assignment]
    agent._narrate_prose = fake_narrate_prose  # ty: ignore[invalid-assignment]
    return captured


# ── 1. 分类字段生效：正则匹配不上的真实案例 ────────────


async def test_player_state_confused_catches_regex_gap() -> None:
    """真人实测撞见的原句："我现在该做什么"——`is_player_confused` 用正则
    匹配不上（"我该"要求字面紧邻，中间插了"现在"），但裁决 LLM 在
    player_state 字段里正确给出 "confused" 时，迷茫引导必须照样生效。"""
    utterance = "我现在该做什么"
    # 先如实确认这句话正是正则的盲区（不这样断言的话，这条测试就证明不了
    # 「分类字段生效而正则本来会漏判」）。
    assert is_player_confused(utterance) is False

    decision = KeeperDecision(
        thinking="玩家在问方向",
        checks=[CheckRequest(skill="侦查", reason="不应保留")],
        narration_guidance="裁决给出的原始指引",
        player_state="confused",
    )
    agent = _keeper()
    captured = _stub_agent(agent, decision)

    room_id, player_id = await _seed_room("PSTAT01")
    context = NarrationContext(
        utterance=utterance,
        player_nickname="阿福",
        room_id=room_id,
        player_id=player_id,
    )
    outcome = await agent.narrate(context)

    final_decision = captured["decision"]
    assert "强制引导" in final_decision.narration_guidance
    assert "裁决给出的原始指引" in final_decision.narration_guidance
    assert final_decision.checks == []
    assert final_decision.san_checks == []
    assert outcome.text == "占位叙事文本。"


# ── 2. 裁决完全失败：退回正则兜底 ────────────────────


async def test_player_state_falls_back_to_regex_when_adjudicate_failed() -> None:
    """裁决走 `_FALLBACK_ADJUDICATE_GUIDANCE` 兜底时，player_state 只是默认值
    "normal"（不可信）——这时必须退回正则判断。用一句正则能命中的迷茫发言
    （"我该怎么办"）证明兜底路径确实生效。"""
    utterance = "我该怎么办"
    assert is_player_confused(utterance) is True  # 正则能命中，这条走兜底

    decision = KeeperDecision(
        thinking="裁决解析失败兜底",
        narration_guidance=_FALLBACK_ADJUDICATE_GUIDANCE,
        player_state="normal",  # 兜底 decision 从不会真的填出分类
    )
    agent = _keeper()
    captured = _stub_agent(agent, decision)

    room_id, player_id = await _seed_room("PSTAT02")
    context = NarrationContext(
        utterance=utterance,
        player_nickname="阿福",
        room_id=room_id,
        player_id=player_id,
    )
    await agent.narrate(context)

    final_decision = captured["decision"]
    assert "强制引导" in final_decision.narration_guidance
    # 兜底文案本身不与迷茫引导拼接（agent.py 327-333 行的既有逻辑）。
    assert "系统兜底" not in final_decision.narration_guidance


# ── 3. player_state 缺省/normal：三个分支都不触发（回归保护） ──


async def test_player_state_normal_does_not_trigger_any_branch() -> None:
    """decision.player_state 为默认值 "normal" 时，即便玩家原话字面上会命中
    "明确行动"的正则（"我去查看书房"），非兜底路径下也**不应该**再看正则——
    分类完全由裁决 LLM 决定，guidance/checks 原样返回。"""
    utterance = "我去查看书房"
    original_checks = [CheckRequest(skill="侦查", reason="裁决已给出的检定")]
    decision = KeeperDecision(
        thinking="正常调查",
        checks=original_checks,
        narration_guidance="裁决给出的原始指引，不应被改动",
        player_state="normal",
    )
    agent = _keeper()
    captured = _stub_agent(agent, decision)

    room_id, player_id = await _seed_room("PSTAT03")
    context = NarrationContext(
        utterance=utterance,
        player_nickname="阿福",
        room_id=room_id,
        player_id=player_id,
    )
    await agent.narrate(context)

    final_decision = captured["decision"]
    assert final_decision.narration_guidance == "裁决给出的原始指引，不应被改动"
    assert [c.skill for c in final_decision.checks] == ["侦查"]


# ── 4. KeeperDecision 的 player_state 解析/降级 ─────────


def test_decision_parses_player_state_field() -> None:
    d = KeeperDecision.model_validate_json(
        '{"thinking": "t", "narration_guidance": "g", "player_state": "weird_or_meta"}'
    )
    assert d.player_state == "weird_or_meta"


def test_decision_player_state_defaults_to_normal_when_missing() -> None:
    """裁决 LLM 没跟上 prompt 变化、没输出这个字段时不能整个解析报错——
    必须安全降级为默认值 "normal"（= 不触发任何特殊分支）。"""
    d = KeeperDecision.model_validate_json('{"thinking": "t", "narration_guidance": "g"}')
    assert d.player_state == "normal"
