"""玩家发起的「我们收工吧」——提议、全桌确认、落幕（2026-08-19）。

## 🔴 为什么它不是收尾门的一部分

收尾（`capabilities/closure`）问的是「**这个故事的内容跑完了没有**」：配对全
揭开、一次性议程全触发。这条路问的是完全不同的另一件事——「**我们还想不想
玩**」。

真人线下团里，收尾最高频的入口根本不是主持人判断出来的，而是玩家自己宣布的
（「我们报警，然后回家」）。而在这之前，系统只给了玩家**否决权**（`ending`
阶段继续说话就退回）和**确认权**（`wrap_up`，且只在门开过之后才消费）——
**没有发起权**。

把两者接到同一个判据上，就是「卡住了和做完了是相反的处境，不能共用一个信号」
那条判据的重演。所以这条路**故意不看任何门**：剧本还剩多少内容不构成反驳，
真人 KP 不会回一句「不行你还有三条线索没查」。

## 协议：任何人提，全体确认，一票否决

「结束」跟掷骰不同——它**作用于整桌人**。一个人替全桌做这个决定，正是
「单人局验不到、多人局才炸」的那一类。

- 其余**在场**的人各一张（`away` 的人不算，见下）；提议者自己不发——他说出口
  就是意思表示。**但只有他一个人在桌上时，那张卡就发给他自己**：发起靠的是
  LLM 判读一句话，而下游是硬墙，总得有一次显式点击挡着（见 `propose_end_game`）。
- **全票才结束**；任何一人拒绝 → 当场清空整批卡，继续玩。
- **没有超时自动确认**：超时自动同意会把「没看见这张卡」变成「同意结束这一
  局」，而这一步之后是硬墙。默认方向必须是**继续玩**（同 `MERGE_CONFIRM_KIND`
  与 `LUCK_SPEND_KIND`）。

## 🔴 「在场」按 `away` 算，不按连接

掉线不等于离场（`left_at` 那次踩过：一列已经有主人就别蹭）。刷个新、地铁进
隧道就被算成"不在桌上、他的票不用等"，那是把网络抖动变成了对局决定。

## 一旦全票通过就是硬墙

不落 `ending` 那个可撤回中间态——**协商已经在确认这一步完成了**。`ending`
存在的理由是「代码替玩家判早了的代价要小」，而这里判断的人就是玩家自己。

不带 `ending_id`：这一局没有命中任何**预设**结局，凭空记一个 id 会造出剧本里
不存在的结局（同 `closure` 那条的处理）。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.keeper.runtime.deps import KeeperToolError
from app.core.keeper.runtime.pending import (
    END_GAME_KIND,
    PendingDecision,
    pending_decision_manager,
)
from app.core.keeper.runtime.phase import (
    PHASE_FINISHED,
    load_phase,
    write_phase,
)
from app.models.room import Player, Room


@dataclass(frozen=True)
class EndGameOutcome:
    """一次提议/表态的结果。调用方（WS 层）据此决定推什么。"""

    #: 这一批新发出去的确认卡（提议时非空，表态时恒空）
    cards: list[PendingDecision]
    #: 全票通过、对局已落 `finished`
    finished: bool = False
    #: 被谁否掉了（拒绝者昵称）；None = 没有人否
    declined_by: str | None = None
    #: 还在等谁（昵称），给玩家看「还差 1 个人」
    waiting_for: tuple[str, ...] = ()


async def _present_players(db: AsyncSession, room_id: str) -> list[Player]:
    """桌上此刻算数的人。`away` 的不算——他已经离场，不该拖住整桌。"""
    rows = await db.execute(select(Player).where(Player.room_id == room_id))
    return [p for p in rows.scalars() if not p.away]


async def propose_end_game(db: AsyncSession, room_id: str, initiator_id: str) -> EndGameOutcome:
    """有人说「收工吧」。给其余在场的人各发一张确认卡。

    🔴 **单人局也要发卡——发给他自己**。"没有别人要点头"不等于"不用确认"：
    发起这条路的是 LLM 对一句话的判读（`player_state == "wrap_up"`），而它
    下游是**硬墙**。角色台词里一句「这事儿也该结束了」被判成收工，单人局就
    当场不可撤回地结束了——原实现把 `wrap_up` 绑死在「收尾门开过」上，正是
    为了防这个。放宽发起条件之后，挡住误判的东西必须换成**一次显式点击**。
    """
    room = await db.get(Room, room_id)
    if room is None:
        raise KeeperToolError("房间不存在")
    if load_phase(room.keeper_state) == PHASE_FINISHED:
        return EndGameOutcome(cards=[])

    # 已经在等这一批了：不重复发（第二个人喊「结束吧」不该再铺一层卡）。
    if await pending_decision_manager.has(db, room_id, {END_GAME_KIND}):
        pending = await pending_decision_manager.list_all(db, room_id, {END_GAME_KIND})
        return EndGameOutcome(cards=[], waiting_for=tuple(d.player_nickname for d in pending))

    present = await _present_players(db, room_id)
    initiator = next((p for p in present if p.id == initiator_id), None)
    if initiator is None:
        raise KeeperToolError("提议的人不在桌上")
    others = [p for p in present if p.id != initiator_id]

    # 没有别人要点头时，这张卡发给提议者自己（理由见 docstring）。
    audience = others or [initiator]
    cards = [
        PendingDecision.end_game(
            room_id=room_id,
            player_id=p.id,
            player_nickname=p.nickname,
            initiator_nickname=initiator.nickname,
            reason=f"{initiator.nickname}提议结束这一局",
        )
        for p in audience
    ]
    await pending_decision_manager.add(db, room_id, cards)
    return EndGameOutcome(cards=cards, waiting_for=tuple(p.nickname for p in others))


async def decide_end_game(
    db: AsyncSession, room_id: str, player_id: str, *, accepted: bool
) -> EndGameOutcome:
    """某个人对「收工吗」表了态。

    拒绝 → **清空整批**（一票否决，不必等其他人再点）。
    同意且是最后一张 → 落 `finished`。
    """
    pending = await pending_decision_manager.list_all(db, room_id, {END_GAME_KIND})
    mine = next((d for d in pending if d.player_id == player_id), None)
    if mine is None:
        raise KeeperToolError("你没有待回答的收工确认")

    if not accepted:
        for card in pending:
            await pending_decision_manager.pop(db, room_id, card.decision_id)
        return EndGameOutcome(cards=[], declined_by=mine.player_nickname)

    await pending_decision_manager.pop(db, room_id, mine.decision_id)
    rest = [d for d in pending if d.decision_id != mine.decision_id]
    if rest:
        return EndGameOutcome(cards=[], waiting_for=tuple(d.player_nickname for d in rest))

    await write_phase(db, room_id, PHASE_FINISHED)
    return EndGameOutcome(cards=[], finished=True)
