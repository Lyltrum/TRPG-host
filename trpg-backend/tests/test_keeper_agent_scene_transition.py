"""场景切换过渡引导：真人实测 2026-07-29——玩家还在跟邻居对话，宣告去
书房，回复直接是"钥匙已经转了半圈、门已经推开"，完全跳过了道别+赶路这段，
读起来像瞬移。

根因是裁决规则 0（"本轮必须推进该行动，人已经走到/做到"）+叙事规则 1
（"正文第一句起就写行动已经发生后的结果"）联手把任何位置切换都压成"立刻
给结果"，没有为"离开当前情境"留一拍。

修法：代码对比"这轮之前 keeper_state 里的当前场景"与"裁决刚写入
state_updates 的新当前场景"，不一样就强制往 narration_guidance 里注入一句
过渡引导——不靠模型自己判断"是不是在对话中途离场"，覆盖所有位置跳变。

不跑真实 LLM——`_adjudicate`/`_narrate_prose` 用实例属性桩掉，只验证
`agent.py::narrate()` 里这段路由逻辑。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.agent import KeeperAgent
from app.core.keeper.decision import KeeperDecision, StateUpdate
from app.core.keeper.module_loader import load_module
from app.core.keeper.phase import PHASE_INVESTIGATION, PHASE_KEY
from app.core.narrator import NarrationContext
from app.models.room import Player, Room

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-scene-transition-test-")) / "agent.db"
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


async def _seed_room(room_code: str, scene: str | None, nickname: str = "阿福") -> tuple[str, str]:
    """建一个已处于调查阶段的房间，keeper_state 里预置"当前场景"（scene 为
    None 时不写这个 key，模拟对局刚开始、还没有任何场景记录的情况）。"""
    keeper_state: dict = {PHASE_KEY: PHASE_INVESTIGATION}
    if scene is not None:
        keeper_state["当前场景"] = scene
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="场景切换测试房",
            max_players=4,
            phase="InGame",
            keeper_state=keeper_state,
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname=nickname)
        db.add(player)
        await db.flush()
        await db.commit()
        return room.id, player.id


def _stub_agent(agent: KeeperAgent, decision: KeeperDecision) -> dict:
    captured: dict = {}

    async def fake_adjudicate(situation: str) -> KeeperDecision:
        return decision

    async def fake_narrate_prose(situation, decision, report, issues, *, max_tokens, max_chars):
        captured["decision"] = decision
        return "占位叙事文本。"

    agent._adjudicate = fake_adjudicate  # ty: ignore[invalid-assignment]
    agent._narrate_prose = fake_narrate_prose  # ty: ignore[invalid-assignment]
    return captured


async def _run(
    room_code: str,
    prev_scene: str | None,
    decision: KeeperDecision,
    *,
    is_heartbeat: bool = False,
):
    agent = _keeper()
    captured = _stub_agent(agent, decision)
    room_id, player_id = await _seed_room(room_code, prev_scene)
    context = NarrationContext(
        utterance="我打算先去那个没动过的书房看一下",
        player_nickname="阿福",
        room_id=room_id,
        player_id=player_id,
        is_heartbeat=is_heartbeat,
    )
    await agent.narrate(context)
    return captured["decision"]


# ── 1. 场景真的变了：必须注入过渡引导 ────────────────────


async def test_scene_change_injects_transition_guidance() -> None:
    decision = KeeperDecision(
        thinking="玩家要去书房",
        narration_guidance="裁决给出的原始指引",
        player_state="clear_action",
        state_updates=[StateUpdate(key="当前场景", value="书房")],
    )
    final_decision = await _run("SCENE01", "邻居家门口", decision)

    assert "场景切换" in final_decision.narration_guidance
    assert "裁决给出的原始指引" in final_decision.narration_guidance


# ── 2. 场景没变：不该注入 ──────────────────────────────


async def test_scene_unchanged_does_not_inject() -> None:
    decision = KeeperDecision(
        thinking="玩家在原地翻找",
        narration_guidance="裁决给出的原始指引",
        player_state="clear_action",
        state_updates=[StateUpdate(key="当前场景", value="书房")],
    )
    final_decision = await _run("SCENE02", "书房", decision)

    assert "场景切换" not in final_decision.narration_guidance
    assert "裁决给出的原始指引" in final_decision.narration_guidance


# ── 3. 对局刚开始、没有任何场景记录：不该注入（没有"离开"这回事）──


async def test_no_prior_scene_does_not_inject() -> None:
    decision = KeeperDecision(
        thinking="第一次进入书房",
        narration_guidance="裁决给出的原始指引",
        player_state="clear_action",
        state_updates=[StateUpdate(key="当前场景", value="书房")],
    )
    final_decision = await _run("SCENE03", None, decision)

    assert "场景切换" not in final_decision.narration_guidance


# ── 4. 这轮裁决没有更新"当前场景"（玩家没有移动）：不该注入 ──────


async def test_no_scene_state_update_does_not_inject() -> None:
    decision = KeeperDecision(
        thinking="玩家原地搜索",
        narration_guidance="裁决给出的原始指引",
        player_state="clear_action",
        state_updates=[],
    )
    final_decision = await _run("SCENE04", "书房", decision)

    assert "场景切换" not in final_decision.narration_guidance


# ── 5. 心跳轮：即便场景变了也跳过（各自已有独立内容约束）──────────


async def test_heartbeat_skips_scene_transition_injection() -> None:
    decision = KeeperDecision(
        thinking="心跳自动推进",
        narration_guidance="心跳原始指引",
        player_state="normal",
        state_updates=[StateUpdate(key="当前场景", value="书房")],
    )
    final_decision = await _run("SCENE05", "邻居家门口", decision, is_heartbeat=True)

    assert "场景切换" not in final_decision.narration_guidance


# ── 6. 叠加：场景切换 + 明确行动可以同时生效，互不覆盖 ────────────


async def test_scene_transition_stacks_with_action_resolution_guidance() -> None:
    decision = KeeperDecision(
        thinking="玩家明确宣告要去书房",
        narration_guidance="",
        player_state="clear_action",
        state_updates=[StateUpdate(key="当前场景", value="书房")],
    )
    final_decision = await _run("SCENE06", "邻居家门口", decision)

    assert "场景切换" in final_decision.narration_guidance
    assert "强制推进" in final_decision.narration_guidance
