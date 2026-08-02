"""待掷检定队列（两段式玩家掷骰，keeper agent 实验）。

裁决判定"要不要检定"后不立即掷骰——真正的骰子由玩家在前端点击按钮确认，
服务端权威生成骰值。裁决产出的检定请求先在这里排队等待玩家确认；一轮回复
可能同时挂起多个（比如一次行动同时触发技能检定 + 目击后的理智检定）。

## 🔴 2026-08-01：从进程内存改为落库（exec/24 §8.1）

原先是进程内存 dict，注释里自称"实验期妥协"。它是个**真 bug**，不是长战役
专属：后端一重启队列就清空，而 `narrate` 有 pending 守卫——玩家等的那张检定
卡片永远不会来，守秘人则一直回「请先完成待掷的检定」，**整局死锁，且没有任何
出路**（重发行动也会撞在守卫上）。

短模组一次跑完暴露不出来；只要对局跨过一次部署/重启就必现。

改动只有存储层：方法名与语义逐字保留，全部变成 `async` 且首参收一个
`AsyncSession`。**排序由自增 `seq` 保证**，不是 `created_at`——同一轮挂起的
多个检定毫秒级时间戳分不出先后，而队列顺序是有意义的。

内存版有个 `requeue_front`（"掷错人"时把 pop 出来的放回队首），落库之后**不再
需要**：pop 只是 flush 未提交，直接 `rollback()` 就等于没发生过，比"再插一行并
手算一个更小的 seq"更不容易错。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.narration.contract import CheckRequestNotice
from app.models.event import PendingCheckRow


@dataclass
class PendingCheck:
    """一次待掷的检定请求。"""

    check_request_id: str  # uuid4
    kind: str  # "skill" | "san"
    room_id: str
    player_id: str  # 房间内部 Player.id
    player_nickname: str
    skill: str | None  # kind="skill" 时的展示名；san 恒为 None
    loss_on_success: str  # kind="san" 时使用；skill 恒 "0"
    loss_on_failure: str
    reason: str
    # 这次检定成功时该揭开哪些事实（来自剧本 ModuleCheck.reveals，exec/14 P4）。
    # 在创建待掷记录时就绑定，而不是结算时再去查——待掷期间场景可能已经变了。
    reveals: tuple[str, ...] = ()
    # 对抗检定（exec/19 #38）：对手的展示名 + 百分位目标值。两个都为 None =
    # 普通检定，结算路径与本功能上线前逐字一致。
    opposed_opponent: str | None = None
    opposed_value: int | None = None


def to_notice(pending: PendingCheck) -> CheckRequestNotice:
    """待掷记录 → 广播用的通知。放在这里而不是 agent.py：断线重连补发
    （ws.py）和裁决时首发（agent.py）用的是同一份转换。"""
    return CheckRequestNotice(
        check_request_id=pending.check_request_id,
        kind=pending.kind,
        player_id=pending.player_id,
        player_nickname=pending.player_nickname,
        skill=pending.skill,
        reason=pending.reason,
    )


def _to_row(check: PendingCheck) -> PendingCheckRow:
    return PendingCheckRow(
        check_request_id=check.check_request_id,
        kind=check.kind,
        room_id=check.room_id,
        player_id=check.player_id,
        player_nickname=check.player_nickname,
        skill=check.skill,
        loss_on_success=check.loss_on_success,
        loss_on_failure=check.loss_on_failure,
        reason=check.reason,
        reveals=list(check.reveals),
        opposed_opponent=check.opposed_opponent,
        opposed_value=check.opposed_value,
    )


def _to_check(row: PendingCheckRow) -> PendingCheck:
    return PendingCheck(
        check_request_id=row.check_request_id,
        kind=row.kind,
        room_id=row.room_id,
        player_id=row.player_id,
        player_nickname=row.player_nickname,
        skill=row.skill,
        loss_on_success=row.loss_on_success,
        loss_on_failure=row.loss_on_failure,
        reason=row.reason,
        reveals=tuple(row.reveals or ()),
        opposed_opponent=row.opposed_opponent,
        opposed_value=row.opposed_value,
    )


class PendingCheckManager:
    """房间级待掷检定队列。**状态在数据库里**，本类无实例状态。

    保留成类而不是一组模块函数，是为了不动 11 处调用点的写法
    （`pending_check_manager.first(...)`），改动只落在"多传一个 db、多一个
    await"上。
    """

    async def add(self, db: AsyncSession, room_id: str, checks: list[PendingCheck]) -> None:
        if not checks:
            return
        for check in checks:
            db.add(_to_row(check))
        await db.flush()

    async def first(self, db: AsyncSession, room_id: str) -> PendingCheck | None:
        row = await db.scalar(
            select(PendingCheckRow)
            .where(PendingCheckRow.room_id == room_id)
            .order_by(PendingCheckRow.seq)
            .limit(1)
        )
        return _to_check(row) if row is not None else None

    async def pop(
        self, db: AsyncSession, room_id: str, check_request_id: str
    ) -> PendingCheck | None:
        """按 id 找到并移除——找不到（已被结算/id 错误）返回 None，不抛异常。"""
        row = await db.scalar(
            select(PendingCheckRow).where(
                PendingCheckRow.room_id == room_id,
                PendingCheckRow.check_request_id == check_request_id,
            )
        )
        if row is None:
            return None
        check = _to_check(row)
        await db.delete(row)
        await db.flush()
        return check

    async def list_all(self, db: AsyncSession, room_id: str) -> list[PendingCheck]:
        """队列里全部待掷检定，按顺序。断线重连要靠它补发检定卡片——
        `check.request` 只在裁决那一刻推过一次，重连的人不补就永远看不到。"""
        rows = await db.scalars(
            select(PendingCheckRow)
            .where(PendingCheckRow.room_id == room_id)
            .order_by(PendingCheckRow.seq)
        )
        return [_to_check(row) for row in rows]

    async def has(self, db: AsyncSession, room_id: str) -> bool:
        row = await db.scalar(
            select(PendingCheckRow.seq).where(PendingCheckRow.room_id == room_id).limit(1)
        )
        return row is not None

    async def clear_room(self, db: AsyncSession, room_id: str) -> None:
        """清空一个房间的队列（对局结束/测试隔离用）。"""
        await db.execute(delete(PendingCheckRow).where(PendingCheckRow.room_id == room_id))
        await db.flush()


pending_check_manager = PendingCheckManager()
