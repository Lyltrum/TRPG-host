"""检定边界硬提醒：真人实测 2026-07-29 三个真实案例——恐吓检定前叙事已经
把 NPC 招供内容写出来了；追踪检定前叙事已经写"沿着小径走了十几步"（检定
对应的动作本身已经在成功进行）；潜行检定前叙事已经写"脚步声被夜风吞掉"
"三十步后你看见了"（同样是动作抢跑）。

旧版指引（折进 narration_guidance 中段）只堵了"提前泄露检定结果信息"这
一个维度，没堵"提前描写检定对应动作已经在成功执行"这第二个维度——三个
真实案例里追踪/潜行两个都是后者。修法：
1. 补齐"动作抢跑"这条禁止项；
2. 挪到 user_content 最末尾（仿 `length_hint` 的位置，近因效应）。

没法用代码保证模型一定服从（"这段话有没有替检定预支结果"不是能靠代码
判断的语义/因果问题），这里只验证：①提示文案内容正确覆盖两个维度；
②`narrate()` 在检定发起分支确实把这段提示传给了 `_narrate_prose` 的
`extra_suffix` 参数（wiring 正确），不验证模型会不会听话。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.agent import KeeperAgent, _build_check_boundary_hint
from app.core.keeper.decision import CheckRequest, KeeperDecision
from app.core.keeper.module_loader import load_module
from app.core.keeper.pending import PendingCheck
from app.core.keeper.phase import PHASE_INVESTIGATION, PHASE_KEY
from app.core.narrator import NarrationContext
from app.models.room import Character, Player, Room

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-check-boundary-test-")) / "agent.db"
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


# ── 1. 纯函数：提示文案内容正确覆盖两个维度 ────────────────


def test_check_boundary_hint_covers_both_information_and_action_dimensions() -> None:
    pending = [
        PendingCheck(
            check_request_id="req1",
            kind="skill",
            room_id="room1",
            player_id="p1",
            player_nickname="阿福",
            skill="追踪",
            loss_on_success="0",
            loss_on_failure="0",
            reason="沿痕迹寻找线索",
        )
    ]
    hint = _build_check_boundary_hint(pending)

    assert "检定边界" in hint
    assert "追踪检定" in hint
    # 维度①：信息（旧版已有）
    assert "不得写出这次检定才能揭示的信息" in hint
    # 维度②：动作本身（这次新补的，真人实测追踪/潜行案例暴露的缺口）
    assert "不得把这次检定对应的动作本身写成已经在成功进行" in hint
    # 通用示例锚定理解，不针对具体技能列举（避免重蹈样本驱动模式匹配的坑）
    assert "情境刚具备、行动才要开始、结果完全悬而未决" in hint


def test_check_boundary_hint_lists_san_check_without_skill_name() -> None:
    pending = [
        PendingCheck(
            check_request_id="req2",
            kind="san",
            room_id="room1",
            player_id="p1",
            player_nickname="阿福",
            skill=None,
            loss_on_success="0",
            loss_on_failure="1d6",
            reason="目击恐怖之物",
        )
    ]
    hint = _build_check_boundary_hint(pending)
    assert "理智检定" in hint


# ── 2. 集成：narrate() 在检定发起分支把提示传给 _narrate_prose 的 extra_suffix ──


async def _seed_room_with_character(room_code: str) -> tuple[str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="检定边界测试房",
            max_players=4,
            phase="InGame",
            keeper_state={PHASE_KEY: PHASE_INVESTIGATION},
        )
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
                },  # fmt: skip
                derived_stats={"HP": 10, "MP": 10, "SAN": 50, "MOV": 8},
                skills={"track": 40},
            )
        )
        await db.commit()
        return room.id, player.id


async def test_narrate_passes_check_boundary_hint_as_extra_suffix() -> None:
    agent = _keeper()
    captured: dict = {}

    async def fake_adjudicate(situation: str) -> KeeperDecision:
        return KeeperDecision(
            thinking="玩家追踪痕迹",
            narration_guidance="裁决给出的原始指引",
            player_state="clear_action",
            checks=[CheckRequest(skill="追踪", reason="沿痕迹寻找线索")],
        )

    async def fake_narrate_prose(
        situation, decision, report, issues, *, max_tokens, max_chars, extra_suffix=""
    ):
        captured["extra_suffix"] = extra_suffix
        captured["narration_guidance"] = decision.narration_guidance
        return "占位叙事文本。"

    agent._adjudicate = fake_adjudicate  # ty: ignore[invalid-assignment]
    agent._narrate_prose = fake_narrate_prose  # ty: ignore[invalid-assignment]

    room_id, player_id = await _seed_room_with_character("CHKBND01")
    context = NarrationContext(
        utterance="我沿着痕迹追踪过去",
        player_nickname="阿福",
        room_id=room_id,
        player_id=player_id,
    )
    outcome = await agent.narrate(context)

    # wiring 正确：extra_suffix 里带着这次检定的边界提示
    assert "检定边界" in captured["extra_suffix"]
    assert "追踪检定" in captured["extra_suffix"]
    # narration_guidance 本身不再被旧版那段"本轮已发起的检定请求"文字污染
    # ——它可能仍然被"明确行动"分支（player_state="clear_action"）注入
    # 强制推进引导，那是两个独立机制的正常叠加（跟场景切换那次一样），
    # 不是这次改动要验证的点；这里只确认原始指引没有被检定边界提示吞掉，
    # 且"检定边界"这几个字没有出现在 narration_guidance 里（它现在只在
    # extra_suffix 里，两条约束分开放，不再拼在一起）。
    assert "裁决给出的原始指引" in captured["narration_guidance"]
    assert "检定边界" not in captured["narration_guidance"]
    assert outcome.check_requests and outcome.check_requests[0].skill == "追踪"


async def test_narrate_normal_turn_forbids_asking_for_rolls() -> None:
    """没有检定发起的普通轮次：追加的是「别要求掷骰」那条硬提醒，且**绝不能**
    混进「已发起以下检定」那条（两条方向相反，同时出现必然把模型搞糊涂）。

    真人实测 2026-07-31（exec/19 #38）：裁决输出 checks=[]，叙事却写出「凌铭辉，
    进行一次体质对抗检定，目标 POT 16」——玩家等一个永远不会出现的骰子卡片。
    """
    agent = _keeper()
    captured: dict = {}

    async def fake_adjudicate(situation: str) -> KeeperDecision:
        return KeeperDecision(
            thinking="玩家闲聊",
            narration_guidance="裁决给出的原始指引",
            player_state="normal",
        )

    async def fake_narrate_prose(
        situation, decision, report, issues, *, max_tokens, max_chars, extra_suffix=""
    ):
        captured["extra_suffix"] = extra_suffix
        return "占位叙事文本。"

    agent._adjudicate = fake_adjudicate  # ty: ignore[invalid-assignment]
    agent._narrate_prose = fake_narrate_prose  # ty: ignore[invalid-assignment]

    room_id, player_id = await _seed_room_with_character("CHKBND02")
    context = NarrationContext(
        utterance="我随便聊聊",
        player_nickname="阿福",
        room_id=room_id,
        player_id=player_id,
    )
    await agent.narrate(context)

    assert "不得要求任何调查员掷骰" in captured["extra_suffix"]
    assert "本轮已发起以下检定" not in captured["extra_suffix"]
