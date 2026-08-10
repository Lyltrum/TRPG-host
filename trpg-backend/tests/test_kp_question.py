"""玩家在问守秘人，不是在角色内行动（exec/19 #40）。

真人实测 2026-07-31：玩家打字「科比特先生在家吗」，问的是守秘人（他忘了这个
设定），叙事却把它演成角色在门厅里喊话——「凌铭辉的声音在门厅里消散，无人
应答」——并且照常写了场景指针。根因是 `player_state` 这条轴上没有"玩家在问
KP"这一类，它只能落进 `normal`，于是走了普通推进路径。

修法是加 `question_to_kp`，命中时**代码强制**把推进世界的手段全部收走
（检定/SAN/移动/场景指针），只留"回答"。分类本身由裁决器判（歧义只能靠语义，
与 #8 迷茫检测同一先例），这里验证的是分类命中后的代码行为。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities.movement.schema import PlayerMove
from app.core.keeper.capabilities.skill_check.schema import CheckRequest
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.runtime.agent import KeeperAgent
from app.core.keeper.runtime.phase import PHASE_INVESTIGATION, PHASE_KEY
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY
from app.core.narration.contract import NarrationContext
from app.models.room import Character, Player, Room

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-kpq-test-")) / "kpq.db"
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


async def _seed(room_code: str) -> tuple[str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="提问房",
            max_players=4,
            phase="InGame",
            keeper_state={PHASE_KEY: PHASE_INVESTIGATION, CURRENT_NODE_KEY: "hall"},
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
                    "CON": 50,
                    "SIZ": 50,
                    "DEX": 70,
                    "APP": 50,
                    "INT": 80,
                    "POW": 50,
                    "EDU": 70,
                    "LUCK": 55,
                },  # fmt: skip
                derived_stats={"HP": 10, "MP": 10, "SAN": 50, "MOV": 8},
                skills={"spot_hidden": 40},
            )
        )
        await db.commit()
        return room.id, player.id


async def _run(room_code: str, player_state: str):
    """裁决器给出一个"想推进世界"的裁决，只有 player_state 这一项不同。"""
    agent = _keeper()
    captured: dict = {}

    async def fake_adjudicate(situation: str) -> KeeperDecision:
        return KeeperDecision(
            thinking="玩家发问",
            narration_guidance="裁决给出的原始指引",
            player_state=player_state,  # ty: ignore[invalid-argument-type]
            checks=[CheckRequest(skill_id="spot-hidden", reason="环顾门厅")],
            current_node_id="cellar",
            # 目标必须跟 current_node_id 不同：两个字段指同一个节点是"只有他去"的
            # 自相矛盾写法，执行层会消解成只跑 moves（房间指针因此不动），
            # 这条用例要验的是"场景照推"，会被那条消解规则遮住。
            moves=[PlayerMove(player="凌铭辉", node_id="hidden-safe")],
        )

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
        captured["guidance"] = decision.narration_guidance
        return "占位叙事文本。"

    agent._adjudicate = fake_adjudicate  # ty: ignore[invalid-assignment]
    agent._narrate_prose = fake_narrate_prose  # ty: ignore[invalid-assignment]

    room_id, player_id = await _seed(room_code)
    outcome = await agent.narrate(
        NarrationContext(
            utterance="科比特先生在家吗",
            player_nickname="凌铭辉",
            room_id=room_id,
            player_id=player_id,
        )
    )
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        node = (room.keeper_state or {}).get(CURRENT_NODE_KEY)
    return outcome, captured, node


async def test_question_to_kp_takes_away_every_way_to_advance_the_world() -> None:
    outcome, captured, node = await _run("KPQ001", "question_to_kp")
    # 提问不发检定
    assert outcome.check_requests == []
    # 提问不挪任何人：场景指针停在原地
    assert node == "hall"
    # 叙事拿到的是"玩家在问你"的指引，且原始指引没被吞掉
    assert "玩家在问你" in captured["guidance"]
    assert "不要把它演成角色的动作或喊话" in captured["guidance"]
    assert "裁决给出的原始指引" in captured["guidance"]


async def test_same_decision_as_normal_still_advances() -> None:
    """对照组：一模一样的裁决，只是分类是 normal → 检定照发、场景照推。

    没有这一条，上面那个测试即使把整条分支删掉也可能碰巧绿。
    """
    outcome, captured, node = await _run("KPQ002", "normal")
    assert [c.skill for c in outcome.check_requests] == ["侦察"]
    assert node == "cellar"
    assert "玩家在问你" not in captured["guidance"]


async def test_feasibility_question_takes_away_every_way_to_advance_the_world() -> None:
    """🔴 exec/25 #59：「我能不能做 X」跟「你还记得吗」共用同一条代码强制。

    真人实测：玩家问「我们能直接去他的地下室吗」被分成 `clear_action`，叙事
    直接推进了剧情。裁决器自己的 thinking 写着"需判断可行性并推进行动"——它
    知道玩家在问可行性，但六个格子里没有这一类。
    """
    outcome, captured, node = await _run("KPQ003", "feasibility_question")
    assert outcome.check_requests == []
    assert node == "hall"
    assert "裁决给出的原始指引" in captured["guidance"]


async def test_feasibility_question_gets_its_own_guidance_not_the_recall_one() -> None:
    """🔴 两类共用"收走推进手段"，但 guidance **必须分开**。

    `question_to_kp` 那段通篇在教叙事器"把他角色应该记得的部分告诉他、用
    「你记得」起头"——那是为**回忆**写的。玩家问「能不能去地下室」时套那段，
    叙事器会去翻他的记忆，而不是回答"能不能、代价是什么"。

    这条独立于上一条：把 `inject` 那行改回永远用 `inject_kp_question_guidance`，
    上一条照样绿（推进手段一样被收走了），只有这条会红。
    """
    _, feasibility, _ = await _run("KPQ004", "feasibility_question")
    _, recall, _ = await _run("KPQ005", "question_to_kp")

    assert "玩家在问能不能" in feasibility["guidance"]
    assert "他还没有决定要做" in feasibility["guidance"]
    # 没有串味：回忆那段的特征句不该出现在可行性这段里
    assert "玩家在问你" not in feasibility["guidance"]
    assert "你记得" not in feasibility["guidance"]
    # 反向同理
    assert "玩家在问能不能" not in recall["guidance"]


async def test_the_decision_itself_gets_recorded() -> None:
    """🔴 exec/25 #61：裁决的分类与理由要落 events 表。

    诊断 #59 时拿不到那一轮 `player_state` 的实际值——state/node/narration 都
    落了表，唯独裁决本身没有，而它才是"叙事为什么这么写"的唯一解释，只能靠
    复现探针推断（而探针复现的是新的一次调用，不是当时那次）。

    `forced` 记的是哪几条代码强制命中了，不是 `narration_guidance` 的内容：
    guidance 里有"须保密什么"，不写进去就永远不会从这条路漏出去。
    """
    from sqlalchemy import select

    from app.models.event import Event

    await _run("KPQ006", "feasibility_question")
    async with _session_factory() as db:
        rows = list(
            (await db.scalars(select(Event).where(Event.event_type == "keeper.decision"))).all()
        )
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["player_state"] == "feasibility_question"
    assert payload["thinking"] == "玩家发问"
    assert "feasibility_question" in payload["forced"]
    # 记的是最终形态：推进手段已被代码收走
    # ⚠️ 键名随 exec/27 阶段 3 的 audit 钩子统一：`check_skill_ids` → `checks`
    # （日志与事件留痕从此共用同一份字段，不再各写一遍）。
    assert payload["checks"] == []
    assert payload["current_node_id"] is None
    # 🔴 guidance 的内容一个字都不落库
    assert "narration_guidance" not in payload
    assert "裁决给出的原始指引" not in str(payload)
