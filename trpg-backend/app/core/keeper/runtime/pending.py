"""待玩家决定队列（`exec/34`）。

**等某个玩家做一个决定；他没决定之前，这一轮不能往下走。**

掷骰是其中一种：裁决判定"要掷"之后不立即掷，真正的骰子由玩家在前端点确认、
服务端权威生成骰值。一轮回复可能同时挂起多个（技能检定 + 目击后的理智检定）。

## 🔴 它曾经叫「待掷检定队列」（exec/34）

那时"等玩家决定"确实只有掷骰一种，所以名字是对的。**第二个实例（会合确认）
出现时没有回头看**，于是另起炉灶各写了一套：状态存 `keeper_state` 自由键、
自己一套推送、**没有重连补发**——`exec/23 #56` 那条「持久化必须配套重连补发」
的教训因此要再学一遍。

> **一个概念被起了它某个实例的名字。** 症状不是 bug，是**重复实现**——
> 不会有任何东西变红，只是每加一个同族功能就要从头写一遍。
>
> 配套的自检问题：给一个队列/表/概念命名时问一句
> **「这是这个概念唯一的形态吗？」**

## 存储：公共列 + payload

表只留所有 kind 共有的列，每种 kind 自己的数据进 `payload`（JSON）。
**加一种 kind 不用加迁移**，也不会出现"一种 kind 占着公共列、别的挤在 JSON 里"
的不对称。

`PendingDecision` 这个 dataclass 反过来给掷骰类提供**只读属性**
（`.skill` / `.reveals` / `.opposed_*`），所以读的那一侧写法不变；
构造走 `PendingDecision.roll(...)`，payload 的形状只在那一个地方拼装。

## 🔴 2026-08-01：从进程内存改为落库（exec/24 §8.1）

原先是进程内存 dict，注释里自称"实验期妥协"。它是个**真 bug**，不是长战役
专属：后端一重启队列就清空，而 `narrate` 有 pending 守卫——玩家等的那张检定
卡片永远不会来，守秘人则一直回「请先完成待掷的检定」，**整局死锁，且没有任何
出路**（重发行动也会撞在守卫上）。

**排序由自增 `seq` 保证**，不是 `created_at`——同一轮挂起的多个毫秒级时间戳
分不出先后，而队列顺序是有意义的。

内存版有个 `requeue_front`（"掷错人"时把 pop 出来的放回队首），落库之后**不再
需要**：pop 只是 flush 未提交，直接 `rollback()` 就等于没发生过。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.narration.contract import CheckRequestNotice
from app.models.event import PendingDecisionRow

#: 掷骰类的 kind。`settler_for(kind)` 按 kind 分发，两者是同一个值域。
ROLL_KINDS = frozenset({"skill", "san"})


@dataclass
class PendingDecision:
    """队列里的一项：等 `player_id` 这个人回答一件事。

    公共字段是所有 kind 都有的；掷骰专属的东西在 `payload` 里，由下面那组
    只读属性取出来——**不是第二份数据**，属性直接读 payload。
    """

    decision_id: str  # uuid4；跨 WS 边界时它就是个不透明令牌
    kind: str  # "skill" | "san" | 将来 "merge_confirm" / "luck_spend"
    room_id: str
    player_id: str  # 房间内部 Player.id
    player_nickname: str
    reason: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def roll(
        cls,
        *,
        kind: str,
        room_id: str,
        player_id: str,
        player_nickname: str,
        reason: str = "",
        skill: str | None = None,
        loss_on_success: str = "0",
        loss_on_failure: str = "0",
        reveals: tuple[str, ...] = (),
        opposed_opponent: str | None = None,
        opposed_value: int | None = None,
        decision_id: str | None = None,
    ) -> PendingDecision:
        """造一项掷骰决定。**payload 的形状只在这里拼**，别处一律走属性读。"""
        return cls(
            decision_id=decision_id or str(uuid.uuid4()),
            kind=kind,
            room_id=room_id,
            player_id=player_id,
            player_nickname=player_nickname,
            reason=reason,
            payload={
                "skill": skill,
                "loss_on_success": loss_on_success,
                "loss_on_failure": loss_on_failure,
                "reveals": list(reveals),
                "opposed_opponent": opposed_opponent,
                "opposed_value": opposed_value,
            },
        )

    # ── 掷骰类的只读视图 ────────────────────────────

    @property
    def skill(self) -> str | None:
        """kind="skill" 时的展示名；san 恒为 None。"""
        return self.payload.get("skill")

    @property
    def loss_on_success(self) -> str:
        return str(self.payload.get("loss_on_success", "0"))

    @property
    def loss_on_failure(self) -> str:
        return str(self.payload.get("loss_on_failure", "0"))

    @property
    def reveals(self) -> tuple[str, ...]:
        """这次检定成功时该揭开哪些事实（来自剧本 `ModuleCheck.reveals`）。

        在创建待掷记录时就绑定，而不是结算时再去查——待掷期间场景可能已经变了。
        """
        return tuple(self.payload.get("reveals") or ())

    @property
    def opposed_opponent(self) -> str | None:
        """对抗检定（exec/19 #38）的对手展示名；普通检定为 None。"""
        return self.payload.get("opposed_opponent")

    @property
    def opposed_value(self) -> int | None:
        """对手的百分位目标值；两个 opposed_* 都为 None = 普通检定。"""
        value = self.payload.get("opposed_value")
        return int(value) if value is not None else None


def to_notice(pending: PendingDecision) -> CheckRequestNotice:
    """待掷记录 → 广播用的通知。放在这里而不是 agent.py：断线重连补发
    （ws.py）和裁决时首发（agent.py）用的是同一份转换。

    ⚠️ 协议字段仍叫 `check_request_id`：改名要前后端一起动，而 `exec/34`
    第 1 步的验收是**行为零变化**。内部叫 `decision_id`，映射在这一行。
    """
    return CheckRequestNotice(
        check_request_id=pending.decision_id,
        kind=pending.kind,
        player_id=pending.player_id,
        player_nickname=pending.player_nickname,
        skill=pending.skill,
        reason=pending.reason,
    )


def _to_row(decision: PendingDecision) -> PendingDecisionRow:
    return PendingDecisionRow(
        decision_id=decision.decision_id,
        kind=decision.kind,
        room_id=decision.room_id,
        player_id=decision.player_id,
        player_nickname=decision.player_nickname,
        reason=decision.reason,
        payload=dict(decision.payload),
    )


def _to_decision(row: PendingDecisionRow) -> PendingDecision:
    return PendingDecision(
        decision_id=row.decision_id,
        kind=row.kind,
        room_id=row.room_id,
        player_id=row.player_id,
        player_nickname=row.player_nickname,
        reason=row.reason,
        payload=dict(row.payload or {}),
    )


class PendingDecisionManager:
    """房间级待决定队列。**状态在数据库里**，本类无实例状态。

    保留成类而不是一组模块函数，是为了不动那十几处调用点的写法
    （`pending_decision_manager.first(...)`）。
    """

    async def add(self, db: AsyncSession, room_id: str, decisions: list[PendingDecision]) -> None:
        if not decisions:
            return
        for decision in decisions:
            db.add(_to_row(decision))
        await db.flush()

    async def first(self, db: AsyncSession, room_id: str) -> PendingDecision | None:
        row = await db.scalar(
            select(PendingDecisionRow)
            .where(PendingDecisionRow.room_id == room_id)
            .order_by(PendingDecisionRow.seq)
            .limit(1)
        )
        return _to_decision(row) if row is not None else None

    async def pop(self, db: AsyncSession, room_id: str, decision_id: str) -> PendingDecision | None:
        """按 id 找到并移除——找不到（已被结算/id 错误）返回 None，不抛异常。"""
        row = await db.scalar(
            select(PendingDecisionRow).where(
                PendingDecisionRow.room_id == room_id,
                PendingDecisionRow.decision_id == decision_id,
            )
        )
        if row is None:
            return None
        decision = _to_decision(row)
        await db.delete(row)
        await db.flush()
        return decision

    async def list_all(self, db: AsyncSession, room_id: str) -> list[PendingDecision]:
        """队列里全部待决定项，按顺序。断线重连要靠它补发卡片——请求只在裁决
        那一刻推过一次，重连的人不补就永远看不到。"""
        rows = await db.scalars(
            select(PendingDecisionRow)
            .where(PendingDecisionRow.room_id == room_id)
            .order_by(PendingDecisionRow.seq)
        )
        return [_to_decision(row) for row in rows]

    async def has(self, db: AsyncSession, room_id: str) -> bool:
        row = await db.scalar(
            select(PendingDecisionRow.seq).where(PendingDecisionRow.room_id == room_id).limit(1)
        )
        return row is not None

    async def clear_room(self, db: AsyncSession, room_id: str) -> None:
        """清空一个房间的队列（对局结束/测试隔离用）。"""
        await db.execute(delete(PendingDecisionRow).where(PendingDecisionRow.room_id == room_id))
        await db.flush()


pending_decision_manager = PendingDecisionManager()
