"""幸运消费的两半：**要不要问**（offer）与**答完之后怎么算**（resolve）。

时序（`exec/34` 第 3、4 步一起做出来的那个窗口）：

```
掷骰（零副作用） → 广播结果（快） → 【这张卡】 → 生效 → 结算叙事
```

跟真人桌上一致：骰子先停，KP 再问。
"""

from __future__ import annotations

import structlog

from app.core.keeper.capabilities.luck_spend.rules import should_offer, spend_cost
from app.core.keeper.primitives import dice
from app.core.keeper.runtime.deps import (
    KeeperDeps,
    KeeperToolError,
    record_event,
    resolve_character,
)
from app.core.keeper.runtime.pending import PendingDecision
from app.core.narration.contract import CheckResultNotice
from app.models.room import Character

logger = structlog.get_logger()

#: 幸运在规则表里是**第 9 项属性**（不是衍生值），所以读写走 `attributes`
#: 而不是 `current_stat`/`write_stat`（那两个只管 `derived_stats`）。
_LUCK_KEY = "LUCK"


def _luck_of(character: Character) -> int:
    attributes: dict = character.attributes or {}
    value = attributes.get(_LUCK_KEY)
    if not isinstance(value, int):
        # 缺数据显式失败，不当成 0：0 会让"花不起"和"没有这项属性"长得一样。
        raise KeeperToolError("角色卡缺少幸运（LUCK）")
    return value


def _write_luck(character: Character, new_value: int) -> None:
    """⚠️ JSON 列必须整体重新赋值——SQLAlchemy 不追踪 dict 的原地修改。"""
    attributes = dict(character.attributes or {})
    attributes[_LUCK_KEY] = new_value
    character.attributes = attributes


async def offer_luck_spend(
    deps: KeeperDeps, roll: PendingDecision, notice: CheckResultNotice
) -> PendingDecision | None:
    """骰子停下之后：要不要问他一句「花幸运吗」。返回 None = 不打扰。"""
    cost = spend_cost(notice.rolled, notice.target)
    try:
        async with deps.session_factory() as db:
            player, character = await resolve_character(db, deps, roll.player_nickname)
            # 🔴 AI 队友不发卡：**没有人能答**，而这张卡挂着的时候整轮停在那儿
            # （`TURN_BLOCKING_KINDS`）——发给 AI 就是把整桌锁死。同族于「AI 没有
            # 连接，那张检定卡片永远等不到点击」（`exec/21` 第三层踩过一次）。
            # 判据是"有没有人能做这个决定"，不是"AI 该不该花幸运"。
            if player.is_ai:
                return None
            luck = _luck_of(character)
    except KeeperToolError as exc:
        # 拿不到幸运值就不问——**不猜一个默认值**。少一次 offer 是可恢复的，
        # 拿假数字算出来的花费不是。
        logger.info("luck_offer_skipped", room_id=deps.room_id, reason=str(exc))
        return None
    if not should_offer(kind=roll.kind, level=notice.level, cost=cost, luck_remaining=luck):
        return None
    return PendingDecision.luck_spend(
        room_id=deps.room_id,
        player_id=roll.player_id,
        player_nickname=roll.player_nickname,
        reason=roll.reason,
        roll=roll,
        notice_payload=_notice_payload(notice),
        cost=cost,
        luck_remaining=luck,
    )


async def resolve_luck_spend(
    deps: KeeperDeps, offer: PendingDecision, accepted: bool
) -> tuple[PendingDecision, CheckResultNotice]:
    """玩家答完了：不花就原样放行，花了就**扣幸运 + 改写结果**。

    🔴 改写不是只把 `level` 改成"成功"：对抗检定的胜负是**比**出来的
    （`dice.resolve_opposed`），所以必须拿新的成功等级**重算一次**。
    推论——**花了幸运也可能还是输**（对手同样成功且技能值更高）。这件事写在
    卡片上，否则玩家花掉 16 点却没赢，只会认为是 bug。
    """
    roll = offer.restore_roll()
    notice = _notice_of(offer)
    if not accepted:
        return roll, notice

    cost = offer.cost
    async with deps.write_lock, deps.session_factory() as db:
        player, character = await resolve_character(db, deps, offer.player_nickname)
        luck = _luck_of(character)
        if luck < cost:
            # 发卡之后幸运被别处扣过了。**不静默降级成"不花"**：玩家点了花，
            # 得到的却是没花，那是最难排查的一类不一致。
            raise KeeperToolError(f"幸运不够了：需要 {cost} 点，现在只有 {luck} 点")
        _write_luck(character, luck - cost)
        await record_event(
            db,
            deps,
            "keeper.luck_spend",
            {
                "player": player.nickname,
                "cost": cost,
                "luck": luck - cost,
                "rolled": notice.rolled,
                "target": notice.target,
            },
        )

    # 花掉 (出目−成功率) 点，正好把出目降到等于成功率 → 普通成功。
    # 「只能推成普通成功」不是一条独立规则，是这个换算的推论，所以这里也不写
    # 成一条特判，直接按降下来的出目重新判等级。
    pushed = dice.evaluate_check(notice.target, notice.target)
    revised = _revise(notice, pushed)
    verdict = (
        "" if revised.opposed_opponent is None else f"，对抗{'胜' if revised.opposed_won else '负'}"
    )
    deps.check_results.append(
        f"{offer.player_nickname} 消耗 {cost} 点幸运（剩 {offer.luck_remaining - cost}）："
        f"{notice.level} → {revised.level}{verdict}"
    )
    return roll, revised


def _revise(notice: CheckResultNotice, pushed: dice.CheckOutcome) -> CheckResultNotice:
    """把推成成功之后的等级（以及对抗胜负）写回结果通知。"""
    opposed_won = notice.opposed_won
    if notice.opposed_level is not None and notice.opposed_target is not None:
        opponent = dice.CheckOutcome(
            rolled=notice.opposed_rolled or 0,
            target=notice.opposed_target,
            level=notice.opposed_level,
        )
        opposed_won = dice.resolve_opposed(pushed, opponent)
    return CheckResultNotice(
        check_request_id=notice.check_request_id,
        kind=notice.kind,
        player_id=notice.player_id,
        skill=notice.skill,
        rolled=notice.rolled,
        target=notice.target,
        level=pushed.level,
        san_loss=notice.san_loss,
        san_remaining=notice.san_remaining,
        opposed_opponent=notice.opposed_opponent,
        opposed_rolled=notice.opposed_rolled,
        opposed_target=notice.opposed_target,
        opposed_level=notice.opposed_level,
        opposed_won=opposed_won,
    )


def _notice_payload(notice: CheckResultNotice) -> dict:
    """结果通知 → 可落库的 dict。**逐个列出的地方**：`CheckResultNotice` 加字段
    要回来加一行，否则玩家答完之后那个字段会静默变回默认值。
    由 `test_luck_spend.py::test_the_notice_survives_a_round_trip` 守着。"""
    return {
        "check_request_id": notice.check_request_id,
        "kind": notice.kind,
        "player_id": notice.player_id,
        "skill": notice.skill,
        "rolled": notice.rolled,
        "target": notice.target,
        "level": notice.level,
        "san_loss": notice.san_loss,
        "san_remaining": notice.san_remaining,
        "opposed_opponent": notice.opposed_opponent,
        "opposed_rolled": notice.opposed_rolled,
        "opposed_target": notice.opposed_target,
        "opposed_level": notice.opposed_level,
        "opposed_won": notice.opposed_won,
    }


def _notice_of(offer: PendingDecision) -> CheckResultNotice:
    return CheckResultNotice(**offer.payload["notice"])
