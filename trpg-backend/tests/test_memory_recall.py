"""L3 召回（`exec/47` P2）：玩家问过去的事，把原文查回来而不是编一个。

起点是 2026-08-24 的实测：模型确信地答「借书卡夹在第 87 页」（真值 88），
而库里那句原文出现过 6 次——**滚出 L3 的是"注入"不是"存储"**。

三层各测各的：
1. `rank` / `tokenize` —— 纯函数，不碰 IO；
2. `recall_history` —— 查库 + **按受众裁**（分头时召不回别人那段的原文）；
3. 🔴 **接线** —— 走完整的 `narrate`，看召回段有没有真的进到叙事拿到的局面块里。
   第 3 层不能省：2026-08-20 的教训是「一整个特性可以只差一根接线就完全不存在」，
   而那次 11 条用例全是 service 层直调，全套 2234 条绿着漏了一整天。
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
from app.core.keeper.memory.recall import (
    MIN_QUERY_COVERAGE,
    format_recall,
    rank,
    recall_history,
    tokenize,
)
from app.core.keeper.runtime.agent import KeeperAgent
from app.core.keeper.runtime.location_state import PLAYER_LOCATION_KEY
from app.core.keeper.runtime.phase import PHASE_INVESTIGATION, PHASE_KEY
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY
from app.core.narration.contract import NarrationContext
from app.models.event import Event
from app.models.room import Player, Room

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")
_db_path = Path(tempfile.mkdtemp(prefix="trpg-recall-test-")) / "recall.db"
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


def test_numbers_and_latin_survive_tokenization() -> None:
    """🔴 车牌与页码正是玩家最会回来问的东西，而 bigram 会把它们切碎。"""
    toks = tokenize("车牌是 KX-4471，借书卡夹在第 88 页")
    assert "88" in toks
    assert any(t.startswith("KX") or t == "KX4471" for t in toks) or "KX4471" in "".join(toks)


def test_rank_finds_the_line_that_holds_the_answer() -> None:
    lines = [
        "阿福：我把屋里看到的记进笔记本。",
        "阿福：我借到一本《缅因州林业志》，随手把自己的借书卡夹在第 88 页当书签。",
        "阿福：我问管理员图书馆几点关门。",
        "阿福：我在第二个岔路口停车，系了一条红布。",
    ]
    hits = rank(lines, "借书卡 林业志 页数", top=2)
    assert any("88" in h for h in hits), hits


def test_rank_returns_nothing_when_the_thing_never_happened() -> None:
    """🔴 问一件本局没发生过的事，**宁可什么都不给**。

    硬塞三行不相干的历史正是编造的原料——那比不给更糟，因为模型会拿它凑。
    """
    lines = [
        "阿福：我把屋里看到的记进笔记本。",
        "阿福：我问管理员图书馆几点关门。",
    ]
    assert rank(lines, "渔船 钓鱼 几斤", top=3) == []


def test_rank_keeps_chronological_order() -> None:
    """召回段是给人读的历史片段，按时间读比按分数读自然。"""
    lines = [
        "阿福：我把借书卡夹在第 88 页。",
        "阿福：我随便看看。",
        "阿福：我又翻了翻那本林业志的第 88 页。",
    ]
    hits = rank(lines, "林业志 借书卡 88", top=2)
    assert hits == [lines[0], lines[2]]


def test_format_recall_is_empty_when_nothing_was_found() -> None:
    """空的时候返回空串——调用方据此整段跳过，局面块逐字节不变。"""
    assert format_recall([]) == ""


def test_format_recall_tells_the_model_what_to_do_when_it_is_not_there() -> None:
    """🔴 一条规则写完先问它有没有反方向：召回**落空**时该怎么办也要写。

    断言选的子串连反例都装不下——把"不要编"改成"可以编"之后这条必须红。
    """
    block = format_recall(["阿福：我把借书卡夹在第 88 页。"])
    assert "只能照这几行原文写" in block
    assert "不要编一个像样的" in block


def test_the_gate_sits_between_the_calibrated_samples() -> None:
    """🔴 阈值要说得出来源：0.4 夹在标定出来的两侧之间（正 0.50–0.75 / 负 0.00–0.29）。

    把它调到 0.29 以下，负样本那条用例就会红；调到 0.5 以上，"纸条内容"那类
    top1 覆盖率 0.50 的真问题会被挡掉。
    """
    assert 0.29 < MIN_QUERY_COVERAGE < 0.5


# ── 2. 查库 + 受众 ─────────────────────────────────


async def _seed_split_room_with_history(room_code: str):
    """两人分头：阿福在门厅、阿贵在地下室，各自说过一句只有自己那边听得见的话。"""
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="召回房",
            max_players=4,
            phase="InGame",
            keeper_state={PHASE_KEY: PHASE_INVESTIGATION, CURRENT_NODE_KEY: "hall"},
        )
        db.add(room)
        await db.flush()
        a = Player(room_id=room.id, nickname="阿福")
        b = Player(room_id=room.id, nickname="阿贵")
        db.add_all([a, b])
        await db.flush()
        room.keeper_state = {**(room.keeper_state or {}), PLAYER_LOCATION_KEY: f"{b.id}@cellar"}
        db.add_all(
            [
                Event(
                    room_id=room.id,
                    player_id=a.id,
                    event_type="action.submit",
                    payload={
                        "utterance": "我把借书卡夹在《缅因州林业志》第 88 页当书签。",
                        "audience": [a.id],
                    },
                ),
                Event(
                    room_id=room.id,
                    player_id=b.id,
                    event_type="action.submit",
                    payload={
                        "utterance": "我把地下室那把备用钥匙埋在第三块石板下。",
                        "audience": [b.id],
                    },
                ),
            ]
        )
        await db.commit()
        return room.id, a.id, b.id


async def test_recall_reaches_lines_that_have_scrolled_out_of_l3() -> None:
    """🔴 这一条就是召回存在的理由：L3 只注入最近 400 条，而这里不设 limit。

    塞 450 条无关事件把那句原文挤出窗口，召回照样把它捞回来。
    """
    room_id, a_id, _b = await _seed_split_room_with_history("RCL001")
    async with _session_factory() as db:
        db.add_all(
            [
                Event(
                    room_id=room_id,
                    player_id=a_id,
                    event_type="action.submit",
                    payload={"utterance": f"我继续往前走第{i}步。", "audience": [a_id]},
                )
                for i in range(450)
            ]
        )
        await db.commit()
    async with _session_factory() as db:
        hits = await recall_history(
            db, room_id=room_id, query="借书卡 林业志 页数", audience=frozenset({a_id})
        )
    assert any("88" in h for h in hits), hits


async def test_recall_is_scoped_to_the_audience() -> None:
    """🔴 保密靠"拿不到"：门厅那段召不回地下室的原文。

    变异检验：把 `visible_history` 那一步去掉，这条立刻红。
    """
    room_id, a_id, b_id = await _seed_split_room_with_history("RCL002")
    async with _session_factory() as db:
        hall = await recall_history(
            db, room_id=room_id, query="钥匙 石板 埋", audience=frozenset({a_id})
        )
        cellar = await recall_history(
            db, room_id=room_id, query="钥匙 石板 埋", audience=frozenset({b_id})
        )
    assert hall == [], f"门厅那段不该召回地下室的原文：{hall}"
    assert any("第三块石板" in h for h in cellar), cellar


# ── 3. 🔴 接线：召回段真的进了叙事拿到的局面块 ──────────


def _keeper() -> KeeperAgent:
    return KeeperAgent(
        api_key="fake-key",
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
    )


def _capture_situations(agent: KeeperAgent, decision: KeeperDecision) -> list[str]:
    situations: list[str] = []

    async def fake_adjudicate(situation: str) -> KeeperDecision:
        return decision

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
        situations.append(situation)
        return "占位叙事。"

    agent._adjudicate = fake_adjudicate  # ty: ignore[invalid-assignment]
    agent._narrate_prose = fake_narrate_prose  # ty: ignore[invalid-assignment]
    return situations


async def _narrate_with(decision: KeeperDecision, room_code: str) -> list[str]:
    agent = _keeper()
    situations = _capture_situations(agent, decision)
    room_id, a_id, _b_id = await _seed_split_room_with_history(room_code)
    await agent.narrate(
        NarrationContext(
            utterance="我把借书卡夹在第几页来着？",
            player_nickname="阿福",
            room_id=room_id,
            player_id=a_id,
        )
    )
    return situations


async def test_the_recalled_lines_reach_the_narrator(monkeypatch) -> None:
    """🔴 守的是接线，不是纯函数：裁决写了 `recall_query` ⇒ 叙事拿到的局面块里
    必须真的有那句原文。

    变异检验：把 `_with_recall` 的调用去掉（或让它原样返回），这条立刻红。
    """
    situations = await _narrate_with(
        KeeperDecision(
            thinking="他在问过去", narration_guidance="继续", recall_query="借书卡 林业志 页数"
        ),
        "RCL003",
    )
    assert len(situations) == 1
    assert "88" in situations[0]
    assert "只能照这几行原文写" in situations[0]


async def test_no_query_means_the_situation_is_untouched() -> None:
    """退化保证：绝大多数拍 `recall_query` 是 None ⇒ 局面块一个字都不多。"""
    situations = await _narrate_with(
        KeeperDecision(thinking="普通行动", narration_guidance="继续"), "RCL004"
    )
    assert len(situations) == 1
    assert "只能照这几行原文写" not in situations[0]
