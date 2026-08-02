"""🔴 表征测试：`agent.narrate` 里那条分类与代码强制 if/elif 链的**完整行为矩阵**。

## 为什么先写它

`exec/27` 阶段 4 要把这条链从 `agent.py` 拆出去。**它的顺序就是语义**
（`asks_kp` 优先于 `confused` 优先于 `weird` 优先于 `clear_action`），而拆错的
后果是**静默改变行为**——没有任何东西会报错，只是某一类发言从此走错分支，
要等真人跑一局才被发现。

所以这份用例不问"应该是什么"，只问"**现在是什么**"：把每一格输入对应的输出
逐条钉死，重构前后必须逐字相同。这是表征测试（characterization test）的定义，
它不评判现有行为对不对——那是别的测试的事。

## 钉的是什么

`keeper.decision` 事件里的 `forced` 字段，它正好是整条链的完整可观测输出：
哪几条代码强制命中了。再加两条外部可见的后果——检定发没发出去、场景推没推进。

覆盖矩阵：7 个 `player_state` × 3 种轮次模式（普通/心跳/开场仪式），
外加裁决兜底与聚光灯两条正交开关。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.agent import _FALLBACK_ADJUDICATE_GUIDANCE, KeeperAgent
from app.core.keeper.capabilities.movement.schema import PlayerMove
from app.core.keeper.capabilities.skill_check.schema import CheckRequest
from app.core.keeper.decision import KeeperDecision
from app.core.keeper.module_loader import load_module
from app.core.keeper.phase import PHASE_INVESTIGATION, PHASE_KEY
from app.core.keeper.scene_state import CURRENT_NODE_KEY
from app.core.narration.contract import NarrationContext
from app.models.event import Event
from app.models.room import Character, Player, Room

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-charac-")) / "c.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)

_PLAYER_STATES = (
    "normal",
    "confused",
    "weird_or_meta",
    "clear_action",
    "question_to_kp",
    "feasibility_question",
    "physical_conflict",
)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(room_code: str) -> tuple[str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="表征房",
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
                skills={"spot-hidden": 60},
            )
        )
        await db.commit()
        return room.id, player.id


async def _observe(
    room_code: str,
    *,
    player_state: str = "normal",
    utterance: str = "我推开书房的门",
    adjudicate_failed: bool = False,
    spotlight: str | None = None,
    is_heartbeat: bool = False,
    is_opening_ceremony: bool = False,
) -> dict:
    """跑一轮，返回这一格的**可观测输出**。

    裁决器被替换成固定输出：它每一轮都想推进世界（发检定、挪场景、带人走），
    这样"哪些手段被代码收走了"才看得出来。
    """
    agent = KeeperAgent(
        api_key="fake-key",
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
    )

    async def fake_adjudicate(situation: str) -> KeeperDecision:
        return KeeperDecision(
            thinking="固定裁决",
            narration_guidance=(
                _FALLBACK_ADJUDICATE_GUIDANCE if adjudicate_failed else "裁决给出的原始指引"
            ),
            player_state=player_state,  # ty: ignore[invalid-argument-type]
            checks=[CheckRequest(skill_id="spot-hidden", reason="环顾")],
            current_node_id="cellar",
            moves=[PlayerMove(player="凌铭辉", node_id="cellar")],
        )

    captured: dict = {}

    async def fake_narrate_prose(
        situation, decision, report, issues, *, max_tokens, max_chars, extra_suffix=""
    ):
        captured["guidance"] = decision.narration_guidance
        return "占位叙事文本。"

    agent._adjudicate = fake_adjudicate  # ty: ignore[invalid-assignment]
    agent._narrate_prose = fake_narrate_prose  # ty: ignore[invalid-assignment]

    room_id, player_id = await _seed(room_code)
    outcome = await agent.narrate(
        NarrationContext(
            utterance=utterance,
            player_nickname="凌铭辉",
            room_id=room_id,
            player_id=player_id,
            spotlight_nickname=spotlight,
            is_heartbeat=is_heartbeat,
            is_opening_ceremony=is_opening_ceremony,
        )
    )
    async with _session_factory() as db:
        # 按房间过滤：同一个数据库里跑多格时不能互相看见对方的事件
        rows = list(
            (
                await db.scalars(
                    select(Event).where(
                        Event.event_type == "keeper.decision", Event.room_id == room_id
                    )
                )
            ).all()
        )
        room = await db.get(Room, room_id)
        assert room is not None
        node = (room.keeper_state or {}).get(CURRENT_NODE_KEY)
    return {
        # 开场仪式轮**不跑裁决**（见下面那条用例），因而没有裁决留痕
        "forced": sorted(rows[0].payload["forced"]) if rows else None,
        "decision_events": len(rows),
        "checks": [c.skill for c in outcome.check_requests],
        "node": node,
        "guidance": captured.get("guidance", ""),
    }


# ── 主矩阵：7 个分类 × 普通轮 ─────────────────────────

#: 每一格的期望值是**现状快照**，不是设计意图。改动它之前先确认那是有意的行为变更。
_NORMAL_TURN = {
    "normal": ([], ["侦察"], "cellar"),
    "confused": (["confused"], [], "cellar"),
    "weird_or_meta": (["weird_or_meta"], [], "cellar"),
    "clear_action": (["clear_action"], ["侦察"], "cellar"),
    "question_to_kp": (["kp_question"], [], "hall"),
    "feasibility_question": (["feasibility_question"], [], "hall"),
    "physical_conflict": (["physical_conflict"], ["侦察"], "cellar"),
}


@pytest.mark.parametrize("player_state", _PLAYER_STATES)
async def test_normal_turn_matrix(player_state: str) -> None:
    """🔴 普通轮：每一格命中哪条强制、检定发不发得出去、场景推不推进。

    `normal` 那一格 `forced` 是**空的**，尽管话术"我推开书房的门"会被正则判成
    明确行动——因为正则只在裁决兜底时才生效，裁决正常时一律信 `player_state`。
    期望值全部来自实际观测（第一版我按"应该是什么"猜，这一格就猜错了）。
    """
    expected_forced, expected_checks, expected_node = _NORMAL_TURN[player_state]
    got = await _observe(f"CH{_PLAYER_STATES.index(player_state)}0", player_state=player_state)
    assert got["forced"] == expected_forced
    assert got["checks"] == expected_checks
    assert got["node"] == expected_node


# ── 心跳 / 开场仪式：两种模式各自的收敛 ────────────────


@pytest.mark.parametrize("player_state", _PLAYER_STATES)
async def test_heartbeat_never_fires_checks_and_never_asks_kp(player_state: str) -> None:
    """心跳轮：检定一律收走；`asks_kp` / `physical_conflict` 一律不成立。"""
    got = await _observe(
        f"CH{_PLAYER_STATES.index(player_state)}1", player_state=player_state, is_heartbeat=True
    )
    assert got["checks"] == []
    assert "kp_question" not in got["forced"]
    assert "feasibility_question" not in got["forced"]
    assert "physical_conflict" not in got["forced"]


@pytest.mark.parametrize("player_state", _PLAYER_STATES)
async def test_opening_ceremony_never_reaches_the_chain_at_all(player_state: str) -> None:
    """🔴 开场仪式轮**根本不跑裁决**——因此整条分类链一次都不执行。

    剧本有 structured 开场素材时，仪式轮直接照【开场脚本】念引子（设计 05）。
    没有裁决就没有 `keeper.decision` 留痕，`player_state` 传什么都不影响。

    这条是第一版写这份表征测试时**猜错**的地方：我按"心跳怎样、开场就怎样"
    去写，实际它连那条链的入口都到不了。**表征测试的价值正在这里**——
    照着以为的行为写，拆的时候就会把"以为"当成契约。
    """
    got = await _observe(
        f"CH{_PLAYER_STATES.index(player_state)}2",
        player_state=player_state,
        is_opening_ceremony=True,
    )
    assert got["decision_events"] == 0
    assert got["forced"] is None
    assert got["checks"] == []


# ── 正交开关：裁决兜底 / 聚光灯 ───────────────────────


async def test_adjudicate_fallback_switches_classification_to_the_regex() -> None:
    """裁决整个失败时 `player_state` 不可信（只是默认值），退回正则兜底。"""
    got = await _observe(
        "CHF01", player_state="normal", utterance="我该做什么", adjudicate_failed=True
    )
    assert "adjudicate_fallback" in got["forced"]
    assert "confused" in got["forced"]
    assert got["checks"] == []


async def test_adjudicate_fallback_drops_the_fallback_text_when_confused() -> None:
    """🔴 兜底文案说"别编造+可请玩家重说"，迷茫引导说"必须给方向"，两句话方向
    相反。拼在一起叙事模型会各退一步、缩回复述已知信息（2026-07-28 实测）。"""
    got = await _observe(
        "CHF02", player_state="normal", utterance="我该做什么", adjudicate_failed=True
    )
    assert _FALLBACK_ADJUDICATE_GUIDANCE not in got["guidance"]
    assert "强制引导" in got["guidance"]


@pytest.mark.parametrize("player_state", ["normal", "confused", "question_to_kp"])
async def test_spotlight_stacks_on_top_of_whatever_branch_won(player_state: str) -> None:
    """聚光灯与三选一**叠加**，不是互斥——被冷落跟他说的是什么类型无关。"""
    got = await _observe(
        f"CHS{_PLAYER_STATES.index(player_state)}", player_state=player_state, spotlight="阿福"
    )
    assert "spotlight" in got["forced"]
    assert "阿福" in got["guidance"]
    # 分支照常判定：`normal` 本来就不命中任何一条，其余两格各自命中自己那条
    expected_extra = {"confused": "confused", "question_to_kp": "kp_question"}.get(player_state)
    assert got["forced"] == sorted(filter(None, ["spotlight", expected_extra]))


# ── 顺序即语义：优先级不能换 ───────────────────────────


async def test_asks_kp_wins_over_the_regex_branches() -> None:
    """🔴 这条钉的是 if/elif 的**顺序**。

    话术本身会被正则判成"明确行动"，但分类是 `question_to_kp` 时必须走提问
    分支——把 `asks_kp` 挪到 `confused`/`weird`/`action_intent` 之后，这条会红。
    """
    got = await _observe("CHO01", player_state="question_to_kp", utterance="我推开书房的门")
    assert got["forced"] == ["kp_question"]
    assert "玩家在问你" in got["guidance"]
    # 提问不推进世界
    assert got["node"] == "hall"
    assert got["checks"] == []
