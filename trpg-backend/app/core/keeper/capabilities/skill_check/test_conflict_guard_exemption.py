"""动手却没裁出检定时，叙事不许替玩家定成败（exec/19 #44）。

定性试玩 2026-07-31 抓到：玩家说「我抄起手边的东西狠狠砸它」，本轮没有任何
检定，叙事照着 `narration_hints.NO_PENDING_CHECK_HINT` 里那句"直接把结果写成既定事实"写了
「科比特侧身一闪，镇纸砸在书架上」——**攻击的成败被叙事定了**。

搜查、移动、对话没检定就直接给结果是对的；**对他人动手不行**：攻击的成败是
玩家花技能点买来的权利，不该由叙事赠予或剥夺。

触发条件是确定的：`player_state == "physical_conflict"` **且** 本轮 pending
为空。分类本身是语义判断（裁决 LLM 做，同 #8 迷茫检测、#40 提问的先例），
"分类命中 + 没有检定 → 换掉那条硬提醒"这一步是代码强制的。这里验证代码那半。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities.skill_check.schema import CheckRequest
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.runtime.agent import KeeperAgent
from app.core.keeper.runtime.phase import PHASE_INVESTIGATION, PHASE_KEY
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY
from app.core.narration.contract import NarrationContext
from app.models.room import Character, Player, Room

#: 🔴 用锚点找，不数层数：`exec/27` 阶段 5 挪目录时 `catalog.py` 的
#: `parents[3]` 当场指错一层，症状只是一条用例**静默 skip**（全套照样绿）。
_TESTS_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "trpg-backend") / "tests"
_FIXTURE_MODULE = str(_TESTS_DIR / "fixtures" / "keeper_module.json")

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-conflict-test-")) / "conflict.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(room_code: str, node_id: str = "hall") -> tuple[str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="冲突房",
            max_players=4,
            phase="InGame",
            keeper_state={PHASE_KEY: PHASE_INVESTIGATION, CURRENT_NODE_KEY: node_id},
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="阿岚")
        db.add(player)
        await db.flush()
        db.add(
            Character(
                room_id=room.id,
                player_id=player.id,
                status="complete",
                name="阿岚",
                occupation="记者",
                age=30,
                gender="男",
                attributes={
                    "STR": 60,
                    "CON": 60,
                    "SIZ": 55,
                    "DEX": 60,
                    "APP": 50,
                    "INT": 70,
                    "POW": 55,
                    "EDU": 65,
                    "LUCK": 55,
                },  # fmt: skip
                derived_stats={"HP": 12, "MP": 11, "SAN": 55, "MOV": 8},
                skills={},
            )
        )
        await db.commit()
        return room.id, player.id


async def _run(
    room_code: str, player_state: str, checks: list[CheckRequest], node_id: str = "hall"
) -> str:
    """跑一轮，返回叙事阶段实际拿到的 extra_suffix。"""
    agent = KeeperAgent(
        api_key="fake-key",
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
    )
    captured: dict = {}

    async def fake_adjudicate(situation: str) -> KeeperDecision:
        return KeeperDecision(
            thinking="玩家动手",
            narration_guidance="裁决给出的原始指引",
            player_state=player_state,  # ty: ignore[invalid-argument-type]
            checks=checks,
        )

    async def fake_narrate_prose(
        situation, decision, report, issues, *, max_tokens, max_chars, extra_suffix=""
    ):
        captured["extra_suffix"] = extra_suffix
        return "占位叙事文本。"

    agent._adjudicate = fake_adjudicate  # ty: ignore[invalid-assignment]
    agent._narrate_prose = fake_narrate_prose  # ty: ignore[invalid-assignment]

    room_id, player_id = await _seed(room_code, node_id)
    await agent.narrate(
        NarrationContext(
            utterance="我抄起手边的东西狠狠砸他",
            player_nickname="阿岚",
            room_id=room_id,
            player_id=player_id,
        )
    )
    return captured["extra_suffix"]


async def test_conflict_without_any_check_forbids_writing_the_outcome() -> None:
    suffix = await _run("CFL001", "physical_conflict", [])
    assert "动手未裁定" in suffix
    assert "绝对不许写出这次动手的成败" in suffix
    assert "停在**动作即将发生的那一刻**" in suffix
    # 两条硬提醒方向相反，绝不能同时出现
    assert "直接把结果写成既定事实" not in suffix


async def test_conflict_with_a_check_keeps_the_normal_path() -> None:
    """裁出了检定 → 主路径：走「检定边界」那条，不是「动手未裁定」。

    这是正常情况——`physical_conflict` 本来就该同时带出格斗/力量检定。

    场景放在 cellar（fixture 里没标注 checks[]，即兴层放行）：门厅标了
    spot-hidden，力量检定会被护栏拦掉——而**护栏拦掉同样算"没裁出检定"**，
    照样触发「动手未裁定」。那是设计意图，不是这条用例要验的东西。
    """
    suffix = await _run(
        "CFL002", "physical_conflict", [CheckRequest(skill_id="STR")], node_id="cellar"
    )
    assert "动手未裁定" not in suffix
    assert "检定边界" in suffix


async def test_non_conflict_without_check_keeps_the_old_hint() -> None:
    """🔴 对照组：同样没有检定，但不是动手 → 仍然是老那条。

    搜查/移动/对话没检定就直接给结果是**对的**，不能被这次改动误伤。
    没有这一条，把整个分支删掉也可能碰巧绿。
    """
    suffix = await _run("CFL003", "clear_action", [])
    assert "动手未裁定" not in suffix
    assert "直接把结果写成既定事实" in suffix


# ── 护栏豁免：动手那一轮不过模组白名单（exec/19 #49）──────────


async def test_conflict_check_is_not_blocked_by_the_module_guard() -> None:
    """🔴 试玩实测抓到的回归：护栏拦掉格斗 → 零检定 → #44 追问 → 玩家再说一次
    → 又被拦 → 又追问。连着两轮问"你是要砸他的头？"，这一拳永远打不出去。

    护栏（设计 02）防的是"用模组没标注的调查技能即兴挖线索"，战斗不在此列——
    模组不可能在每个节点标注格斗检定点，而玩家有权动手。

    门厅只标注了 spot-hidden；`physical_conflict` 轮的格斗检定必须照样发出。
    """
    suffix = await _run(
        "CFL004", "physical_conflict", [CheckRequest(skill_id="fighting-brawl")], node_id="hall"
    )
    assert "动手未裁定" not in suffix
    assert "检定边界" in suffix


async def test_non_conflict_check_is_still_blocked_by_the_guard() -> None:
    """对照组：不是动手的轮次，护栏照常拦——豁免不能扩大成放水。"""
    suffix = await _run(
        "CFL005", "clear_action", [CheckRequest(skill_id="library-use")], node_id="hall"
    )
    # 被护栏拦掉 → 本轮零检定 → 走「没有待掷检定」那条老提醒
    assert "直接把结果写成既定事实" in suffix
    assert "检定边界" not in suffix
