"""玩家发起的「我们收工吧」（2026-08-19）。

设计与判据写在 `app/core/keeper/runtime/end_game.py` 的模块文档里。这里守四件事：
**全票才结束**、**一票否决**、**单人局也要点一次**、**它不看收尾门**。
"""

from __future__ import annotations

import pytest

from app.core.keeper.runtime.end_game import decide_end_game, propose_end_game
from app.core.keeper.runtime.pending import END_GAME_KIND, pending_decision_manager
from app.core.keeper.runtime.phase import (
    PHASE_FINISHED,
    PHASE_INVESTIGATION,
    PHASE_KEY,
    load_phase,
)
from app.models.room import Player, Room


async def _room(db, *nicknames: str, phase: str = PHASE_INVESTIGATION) -> tuple[Room, list[Player]]:
    room = Room(
        room_code="END001",
        room_name="收工测试",
        max_players=4,
        phase="InGame",
        keeper_state={PHASE_KEY: phase},
    )
    db.add(room)
    await db.flush()
    players = []
    for i, nickname in enumerate(nicknames):
        p = Player(room_id=room.id, nickname=nickname, is_host=(i == 0))
        db.add(p)
        players.append(p)
    await db.flush()
    return room, players


@pytest.mark.asyncio
async def test_everyone_else_gets_a_card_the_proposer_does_not(db_session) -> None:
    """提议者说出口就是他的意思表示，不必再问他一遍。"""
    room, (alice, bob, carol) = await _room(db_session, "程雨眠", "霍启元", "邵行远")

    outcome = await propose_end_game(db_session, room.id, alice.id)

    assert {c.player_id for c in outcome.cards} == {bob.id, carol.id}
    assert outcome.finished is False
    assert set(outcome.waiting_for) == {"霍启元", "邵行远"}
    assert all(c.payload["initiator"] == "程雨眠" for c in outcome.cards)


@pytest.mark.asyncio
async def test_the_game_ends_only_after_the_last_person_agrees(db_session) -> None:
    """🔴 **全票才结束**。这是这条路的全部理由——「结束」作用于整桌人，
    一个人替全桌决定正是「单人局验不到、多人局才炸」的那一类。

    **变异检验**：把 `decide_end_game` 里的 `if rest:` 去掉（第一个人同意就
    收束），这条当场红。
    """
    room, (alice, bob, carol) = await _room(db_session, "程雨眠", "霍启元", "邵行远")
    await propose_end_game(db_session, room.id, alice.id)

    first = await decide_end_game(db_session, room.id, bob.id, accepted=True)
    assert first.finished is False
    assert first.waiting_for == ("邵行远",)
    await db_session.flush()
    assert load_phase(room.keeper_state) == PHASE_INVESTIGATION

    second = await decide_end_game(db_session, room.id, carol.id, accepted=True)
    assert second.finished is True
    await db_session.flush()
    assert load_phase(room.keeper_state) == PHASE_FINISHED


@pytest.mark.asyncio
async def test_one_refusal_cancels_the_whole_batch(db_session) -> None:
    """🔴 一票否决，且**当场清空整批**——不必等其他人再点一遍。"""
    room, (alice, bob, carol) = await _room(db_session, "程雨眠", "霍启元", "邵行远")
    await propose_end_game(db_session, room.id, alice.id)

    outcome = await decide_end_game(db_session, room.id, bob.id, accepted=False)

    assert outcome.declined_by == "霍启元"
    assert outcome.finished is False
    assert await pending_decision_manager.list_all(db_session, room.id, {END_GAME_KIND}) == []
    await db_session.flush()
    assert load_phase(room.keeper_state) == PHASE_INVESTIGATION


@pytest.mark.asyncio
async def test_a_solo_player_still_has_to_click_once(db_session) -> None:
    """🔴 **单人局也发卡——发给他自己。**

    发起这条路是 LLM 对一句话的判读（`player_state == "wrap_up"`），下游是硬墙。
    「没有别人要点头」不等于「不用确认」：角色台词里一句「这事儿也该结束了」
    被判成收工，单人局就当场不可撤回地结束了。

    **变异检验**：把 `audience = others or [initiator]` 改回「没有别人就直接
    `write_phase(FINISHED)`」，这条当场红。
    """
    room, (solo,) = await _room(db_session, "程雨眠")

    outcome = await propose_end_game(db_session, room.id, solo.id)

    assert outcome.finished is False
    assert [c.player_id for c in outcome.cards] == [solo.id]
    await db_session.flush()
    assert load_phase(room.keeper_state) == PHASE_INVESTIGATION

    done = await decide_end_game(db_session, room.id, solo.id, accepted=True)
    assert done.finished is True


@pytest.mark.asyncio
async def test_away_players_do_not_hold_the_table_hostage(db_session) -> None:
    """已经离场的人不该拖住整桌。🔴 注意判的是 `away` 不是连接——
    掉线不等于离场（`left_at` 那次踩过）。"""
    room, (alice, bob, gone) = await _room(db_session, "程雨眠", "霍启元", "邵行远")
    gone.away = True
    await db_session.flush()

    outcome = await propose_end_game(db_session, room.id, alice.id)

    assert [c.player_id for c in outcome.cards] == [bob.id]


@pytest.mark.asyncio
async def test_a_second_proposal_does_not_stack_another_layer_of_cards(db_session) -> None:
    """第二个人也喊「结束吧」时不重复铺卡——否则每个人手上会攒下一叠。"""
    room, (alice, bob, carol) = await _room(db_session, "程雨眠", "霍启元", "邵行远")
    await propose_end_game(db_session, room.id, alice.id)

    again = await propose_end_game(db_session, room.id, carol.id)

    assert again.cards == []
    assert len(await pending_decision_manager.list_all(db_session, room.id, {END_GAME_KIND})) == 2


@pytest.mark.asyncio
async def test_it_does_not_look_at_the_closure_gate(db_session) -> None:
    """🔴 **这条路故意不看任何门。**

    收尾门问的是「内容跑完了没有」，这张卡问的是「我们还想不想玩」——两个独立
    的信号。剧本还剩多少内容不构成反驳：真人 KP 不会回一句「不行你还有三条
    线索没查」。

    这里连模组都没有（`propose_end_game` 的签名里就没有它），配对/议程无从查起
    ——**签名本身就是这条判据的守卫**。传得进去，早晚有人会去查它。
    """
    import inspect

    params = set(inspect.signature(propose_end_game).parameters)
    assert "module" not in params
    assert params == {"db", "room_id", "initiator_id"}


@pytest.mark.asyncio
async def test_finished_rooms_ignore_new_proposals(db_session) -> None:
    """已经结束的局再喊一次收工是空操作，不该再铺一批没人点的卡。"""
    room, (alice, bob) = await _room(db_session, "程雨眠", "霍启元", phase=PHASE_FINISHED)

    outcome = await propose_end_game(db_session, room.id, alice.id)

    assert outcome.cards == []
    assert outcome.finished is False


# ── 提前收场时把谜底交代出来 ────────────────────────────────


_MODULE_FIXTURE = {
    "meta": {"id": "end-game-fixture", "title": "试验模组"},
    "kp_truth": {"summary": "无。", "key_facts": ["科比特一直活着", "地窖下面另有一层"]},
    "player_intro": "你在街上。",
    "nodes": [
        {"id": "cellar", "title": "地窖", "kp_text": "墙后藏着第二间实验室。"},
        {"id": "attic", "title": "阁楼", "kp_text": "这里什么都没有。"},
    ],
    "visibility_pairs": [
        {"id": "pair-1", "public_ref": "rumor", "secret_ref": "cellar"},
        {"id": "pair-2", "public_ref": "noise", "secret_ref": "attic"},
        # 真相侧指向一个 NPC（不是节点）：玩家结构上揭不开，不该算进"没查到"
        {"id": "pair-3", "public_ref": "shadow", "secret_ref": "npc-1"},
    ],
}


def _fixture_module():
    from app.core.keeper.contract.module_loader import ScenarioModule

    return ScenarioModule.model_validate(_MODULE_FIXTURE)


def test_missed_truths_lists_core_facts_and_unrevealed_pairs() -> None:
    """🔴 真人 KP 收场时一定会把谜底讲出来，那是玩家最在乎的部分。

    而 `kp_truth` 此前**只进裁决 prompt**，没有任何通往玩家的出口。
    """
    from app.service.recap import build_missed_truths

    out = build_missed_truths(_fixture_module(), {})

    assert "科比特一直活着" in out
    assert "地窖下面另有一层" in out
    # 配对的真相侧是**节点 id**，要解引用才拿得到内容（不是一个现成的 truth 字段）
    assert any("地窖" in line and "第二间实验室" in line for line in out)


def test_already_revealed_pairs_are_not_repeated() -> None:
    """已经查到的不算"没查到"——否则这份清单会把玩家自己挣来的发现也算进遗憾。"""
    from app.core.keeper.runtime.progress_state import CLUES_REVEALED_KEY
    from app.service.recap import build_missed_truths

    # `@邵行远` 是**个人级**揭开：只有他一个人发现。它照样算"查到了"——
    # 复盘是给全桌看的终局交代，不该把某个人挣来的发现写成遗憾。
    out = build_missed_truths(_fixture_module(), {CLUES_REVEALED_KEY: "pair-1@邵行远"})

    assert not any("第二间实验室" in line for line in out)
    assert any("阁楼" in line for line in out)


def test_structurally_unreachable_pairs_are_excluded() -> None:
    """真相侧指向 NPC 的那些**玩家永远揭不开**，列进"你们没查到"是误导。

    （同 `reachable_visibility_pairs` 的判据——那正是 08-14 收尾门量端点量出来的。）
    """
    from app.service.recap import build_missed_truths

    out = build_missed_truths(_fixture_module(), {})

    assert not any("npc-1" in line for line in out)


@pytest.fixture
def _stub_module(monkeypatch):
    """让 `resolve_module` 一定解析得出模组。

    🔴 **不打这个桩，那道门的测试就是假的**：第一版直接喂了一个内置 scenario id，
    而测试环境解析不出模组 ⇒ 函数在**模组那一支**就返回了 `None`，于是把 phase
    判断整个删掉，测试照样绿（变异检验当场抓到）。
    **造的样本没走到被测分支 = 没测。**
    """
    from app.core.keeper.contract.source import ResolvedModule

    async def _fake(_db, _dir, scenario_id):
        return ResolvedModule(cache_key="stub", module=_fixture_module()) if scenario_id else None

    monkeypatch.setattr("app.service.recap.resolve_module", _fake)


@pytest.mark.asyncio
async def test_the_spoilers_stay_shut_until_the_game_is_over(db_session, _stub_module) -> None:
    """🔴 **这一条是那道门本身。**

    `GET /summary` **允许中途查看**（服务层注释：「还在跑的局也允许看复盘」），
    不设门它当场变成剧透工具。

    两头都验：局还在跑 → `None`；局结束了 → 真的给得出内容。只验一头的话，
    "永远返回 None" 这个变异体活得下来。
    """
    from app.service.recap import _missed_truths_if_finished

    room, _ = await _room(db_session, "程雨眠", phase=PHASE_INVESTIGATION)
    room.scenario_id = "end-game-fixture"
    await db_session.flush()

    assert await _missed_truths_if_finished(db_session, room) is None

    room.keeper_state = {**(room.keeper_state or {}), PHASE_KEY: PHASE_FINISHED}
    await db_session.flush()

    out = await _missed_truths_if_finished(db_session, room)
    assert out is not None
    assert "科比特一直活着" in out
