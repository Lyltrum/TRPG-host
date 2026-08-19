"""收尾是可撤回的：`ending` 阶段里玩家继续说话 = 「我们还想玩」。

## 🔴 为什么需要它（2026-08-12 真人反馈）

「我可以一直玩、一直偏离主线，但永远不会被 AI 主持人说已经结束了。」

原先自然收尾直接落 `finished`，而那是**一堵硬墙**（`agent.py` 直接返回"本局
已结束"，模型都不再跑）。收早了极贵，于是规则 10b 给 KP 加了一道「三个数都
见底才准收」的机械前提——**代码替 KP 做了它本来就该做的判断**。而玩家在原地
打转时那三个数永远不见底，落幕就永远等不到。

修法不是把阈值调准，是让**判错的代价变小**：落在 `ending`，玩家接着行动就
自动退回 `investigation`。边界画不准就不必画准了。

配套的另一半在 `capabilities/closure/`（收尾落在 ending + 停滞轮数）。
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
from app.core.keeper.runtime.agent import KeeperAgent
from app.core.keeper.runtime.phase import (
    PHASE_ENDING,
    PHASE_FINISHED,
    PHASE_INVESTIGATION,
    PHASE_KEY,
    load_phase,
)
from app.core.narration.contract import NarrationContext, PlayerUtterance
from app.models.room import Player, Room

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")

_db_path = Path(tempfile.mkdtemp(prefix="trpg-closure-reopen-test-")) / "reopen.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(room_code: str, phase: str) -> tuple[str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="收尾房",
            max_players=4,
            phase="InGame",
            keeper_state={PHASE_KEY: phase, "当前场景": "门厅"},
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="阿福")
        db.add(player)
        await db.flush()
        await db.commit()
        return room.id, player.id


def _agent(player_state="normal", captured: dict | None = None) -> KeeperAgent:  # noqa: ANN001
    agent = KeeperAgent(
        api_key="fake-key",
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
    )

    async def fake_adjudicate(situation: str) -> KeeperDecision:
        return KeeperDecision(thinking="桩", narration_guidance="继续", player_state=player_state)

    async def fake_narrate_prose(situation, decision, *args, **kwargs) -> str:
        if captured is not None:
            captured["guidance"] = decision.narration_guidance
        return "阁楼的门在你手下吱呀作响。"

    agent._adjudicate = fake_adjudicate  # ty: ignore[invalid-assignment]
    agent._narrate_prose = fake_narrate_prose  # ty: ignore[invalid-assignment]
    return agent


def _ctx(room_id: str, player_id: str, utterance: str, **kw) -> NarrationContext:
    return NarrationContext(
        utterance=utterance,
        player_nickname="阿福",
        room_id=room_id,
        player_id=player_id,
        utterances=(PlayerUtterance(player_id=player_id, nickname="阿福", text=utterance),),
        **kw,
    )


async def _phase_of(room_id: str) -> str | None:
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        return load_phase(room.keeper_state)


async def test_a_player_speaking_up_reopens_the_story() -> None:
    """「等等，我还想上阁楼」——这句话本身就是意思表示，不需要额外交互。"""
    room_id, player_id = await _seed("RPN100", PHASE_ENDING)

    outcome = await _agent().narrate(_ctx(room_id, player_id, "等等，我还想上阁楼看看"))

    assert await _phase_of(room_id) == PHASE_INVESTIGATION
    # 退回之后这一轮要**照常主持**，不是回一句"本局已结束"
    assert "本局已结束" not in outcome.text
    assert outcome.text.strip() != ""


async def test_the_heartbeat_does_not_reopen_a_closing_story() -> None:
    """心跳不是玩家的意思表示。让它退回，等于世界自己把自己的落幕撤销掉。"""
    room_id, player_id = await _seed("RPN200", PHASE_ENDING)

    ctx = _ctx(room_id, player_id, "（时间悄然流逝）", is_heartbeat=True)
    outcome = await _agent().narrate(ctx)

    assert await _phase_of(room_id) == PHASE_ENDING
    assert outcome.text == ""


async def test_a_finished_game_is_still_a_hard_wall() -> None:
    """退化保证：命中**剧本预设结局**那条路仍然直达 finished，且拒收行动。
    可撤回的只有"KP 自己觉得该停了"那一种。"""
    room_id, player_id = await _seed("RPN300", PHASE_FINISHED)

    outcome = await _agent().narrate(_ctx(room_id, player_id, "我还想上阁楼"))

    assert await _phase_of(room_id) == PHASE_FINISHED
    assert "本局已结束" in outcome.text


# ── 出戏地想收场 → 真的收（2026-08-15）─────────────────
#
# 🔴 上面那条退回规则假设「玩家继续说话 = 还想玩」。08-14 实测里它正好反过来：
# 玩家已经回城复命完毕，连说三次「可以结束了」「结束了吧」，每一次都被判成
# 还想玩，**对局就是结束不了**。那三句不是角色台词，是他抬起头跟主持人讲话
# ——**出戏的话被当成戏内发言喂进了裁决**。


async def _confirm_end_game(room_id: str, player_id: str) -> None:
    """替玩家点掉那张「收工吗」确认卡。"""
    from app.core.keeper.runtime.end_game import decide_end_game

    async with _session_factory() as db:
        await decide_end_game(db, room_id, player_id, accepted=True)
        await db.commit()


async def test_an_out_of_character_wrap_up_really_ends_the_game() -> None:
    """🔴 **2026-08-19：多了一步确认。**

    `wrap_up` 不再直接落 `finished`，它现在**发一张全桌确认卡**——「结束」
    作用于整桌人，一个人替全桌决定正是「单人局验不到、多人局才炸」的那一类。
    单人局也要点一次（发起靠的是 LLM 判读一句话，下游是硬墙）。

    这条守的东西没变：**出戏说"结束了吧"最终真的能结束对局**。
    """
    room_id, player_id = await _seed("RPN400", PHASE_ENDING)

    await _agent(player_state="wrap_up").narrate(_ctx(room_id, player_id, "结束了吧"))
    assert await _phase_of(room_id) != PHASE_FINISHED  # 还没点，不许自己结束

    await _confirm_end_game(room_id, player_id)
    assert await _phase_of(room_id) == PHASE_FINISHED


async def test_the_next_action_after_a_wrap_up_hits_the_wall() -> None:
    """收完之后再说话就该撞墙——不然"结束"只是句空话。"""
    room_id, player_id = await _seed("RPN401", PHASE_ENDING)
    await _agent(player_state="wrap_up").narrate(_ctx(room_id, player_id, "结束了吧"))
    await _confirm_end_game(room_id, player_id)

    outcome = await _agent().narrate(_ctx(room_id, player_id, "我还想再看一眼"))

    assert "本局已结束" in outcome.text


async def test_a_wrap_up_mid_game_proposes_but_does_not_end_anything() -> None:
    """🔴 **判据换了形状（2026-08-19），但守的东西更强了。**

    原来这条叫「收尾门没开过就不算数」：中途喊一句不玩了会被**整个忽略**，
    因为那时挡住误判的东西是"门先得开过"。现在玩家有了发起权（真人线下团里
    收尾最高频的入口就是玩家自己宣布的），挡误判的换成了**一次显式点击**。

    所以中途 `wrap_up` **会发卡、但绝不自己结束**。没有这一条，把
    `decide_end_game` 改成"提议即结束"也会绿。
    """
    from app.core.keeper.runtime.pending import END_GAME_KIND, pending_decision_manager

    room_id, player_id = await _seed("RPN402", PHASE_INVESTIGATION)

    await _agent(player_state="wrap_up").narrate(_ctx(room_id, player_id, "结束了吧"))

    assert await _phase_of(room_id) == PHASE_INVESTIGATION
    async with _session_factory() as db:
        cards = await pending_decision_manager.list_all(db, room_id, {END_GAME_KIND})
    assert [c.player_id for c in cards] == [player_id]


async def test_reopening_still_works_for_anything_else() -> None:
    """退化保证：不是 `wrap_up` 的发言仍然退回调查阶段。"""
    room_id, player_id = await _seed("RPN403", PHASE_ENDING)

    await _agent(player_state="clear_action").narrate(_ctx(room_id, player_id, "我还想上阁楼"))

    assert await _phase_of(room_id) == PHASE_INVESTIGATION


# ── ending 阶段的收束纪律（此前一条都没有）──────────────


async def test_the_closing_turn_gets_closure_discipline() -> None:
    """🔴 `ending` 阶段此前的全部效果只有：局面块多一行 + 叙事字数上限放宽。

    于是玩家进了收尾阶段收到的还是一段普通调查叙事，跟没进一样。真人 KP 的
    调研结论第 4 条写着「真收尾前会先铺垫一个收束场景，给玩家最后的动作机会」
    ——那正是这里缺的一拍。
    """
    room_id, player_id = await _seed("RPN500", PHASE_ENDING)
    captured: dict = {}

    await _agent(captured=captured).narrate(_ctx(room_id, player_id, "我看看四周"))

    assert "收束·代码注入" in captured["guidance"]
    assert "不要再抛出任何新线索" in captured["guidance"]


async def test_an_ordinary_turn_gets_no_closure_discipline() -> None:
    """对照组：没进收尾阶段的普通轮次不该被塞收场指令。"""
    room_id, player_id = await _seed("RPN501", PHASE_INVESTIGATION)
    captured: dict = {}

    await _agent(captured=captured).narrate(_ctx(room_id, player_id, "我看看四周"))

    assert "收束·代码注入" not in captured.get("guidance", "")
