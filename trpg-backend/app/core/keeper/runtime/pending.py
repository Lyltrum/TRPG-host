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

#: 掷骰类的 kind。`settle_hook_for(kind)` 按 kind 分发，两者是同一个值域。
ROLL_KINDS = frozenset({"skill", "san"})

#: 「要不要花幸运把这次失败推成成功」（`exec/26 #66`，`exec/34` 第 4 步）。
#:
#: 它挂在**骰子已经停下、但结果还没生效**的那个窗口里：掷骰 → 广播 → **这张卡**
#: → 生效 → 结算叙事。所以它跟掷骰一样，**没决定之前这一轮不能往下走**。
#:
#: 🔴 **没有超时**。`#66` 的初稿写着"超时按不花继续"，落地时改成不设，理由是
#: 一致性：掷骰本身就没有超时（玩家不点「投掷」，这一轮同样停在那儿），而幸运
#: 决定是同一个窗口里的同一件事。给它单独造一套定时器，等于**为一个用户造一个
#: 框架**，而且会让"两种等待、两种超时语义"成为下一个人要维护的分叉。
LUCK_SPEND_KIND = "luck_spend"

#: 「这一轮还能不能往下走」的判据——**守秘人叙事的守卫用它**，不是 `ROLL_KINDS`。
#:
#: 🔴 加一种 kind 就要回来看这里一眼：会合确认**故意不在**里面（那张卡按设计
#: 可以一直挂着，算进来整桌就说不了话），而幸运卡**必须在**里面（它挂着的时候
#: 有一次检定的结果悬而未决，放行就等于让世界跑在一个还没定的结果前面）。

TURN_BLOCKING_KINDS = ROLL_KINDS | {LUCK_SPEND_KIND}

#: 「你跟他们碰上了吗」——分组变更协议里那张要当事人点头的卡（`exec/33 §5`）。
#:
#: ## 🔴 为什么需要它：分组此前是概率性的
#:
#: 「谁跟谁在一处」由裁决器**每轮重写**的位置派生，于是**每轮都有一次写错分组的
#: 机会**。2026-08-10 多人实测实证：它把 `current_node_id` 与 `moves` 写矛盾，
#: 被明确留下的队友被拖进地下室 → 系统认为没分头 → 全房间广播是**完全正确的
#: 执行**，只是建立在错的位置上。投递层再结构化也没用——**保证等于最弱的那一环**。
#:
#: ## 🔴 协议是不对称的，因为两个方向的错误代价不同
#:
#: - **分开**判错 → 多隔离了一个人：困惑、可恢复、**不泄露** → 乐观执行，不打断。
#: - **会合**判错 → 两组信息当场合并：**泄露、不可撤回** → 必须由**当事人**确认。
#:
#: 这是「受众算错必须朝保密方向失败，绝不退化成广播」的直接应用。确认之所以
#: 合法，是因为问的是当事人自己知道的事（"你走回客厅跟大家会合了吗"）——
#: 跟被否掉的「房主确认结局」正相反，那次是问一个按设计就不该有信息的人。
#:
#: **位置照常写**（它仍是唯一地基，不新增第二份真相）；这张卡只让 `group_players`
#: 在**投递侧**保守一点：没确认之前，这个人自己一组。
#:
#: **没有超时自动确认**：超时自动 = 静默泄露。也没有"否认"动作，不点就是维持分离。
MERGE_CONFIRM_KIND = "merge_confirm"


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
        target: int | None = None,
        decision_id: str | None = None,
    ) -> PendingDecision:
        """造一项掷骰决定。**payload 的形状只在这里拼**，别处一律走属性读。

        `target`：这次要过的数。**掷之前就告诉玩家**（2026-08-16）——
        `CheckRequestPayload.target_value` 这个字段一直存在、`check.result`
        那边也一直在填，唯独 `check.request` 这边写死 `None` 且没有注释，
        前端卡片于是只写「守秘人请求：XX检定」。玩家掷之前不知道自己要过多少，
        而这个数本来就印在他自己的角色卡上，藏着没有任何收益。
        理智检定不带（它比的是当前 SAN，掷的那一刻才算得准）。
        """
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
                "target": target,
            },
        )

    @classmethod
    def merge_confirm(
        cls,
        *,
        room_id: str,
        player_id: str,
        player_nickname: str,
        reason: str = "",
        decision_id: str | None = None,
    ) -> PendingDecision:
        """造一张会合确认卡。

        它**不带位置**：位置只有一份真相（`玩家位置`），在卡里再存一份拷贝的
        那一版实测出过 bug——全组一起换场景时人还在一起，卡却被判过期丢弃，
        于是**没人点头就合并了**（`exec/33 §5`）。
        """
        return cls(
            decision_id=decision_id or str(uuid.uuid4()),
            kind=MERGE_CONFIRM_KIND,
            room_id=room_id,
            player_id=player_id,
            player_nickname=player_nickname,
            reason=reason,
        )

    @classmethod
    def luck_spend(
        cls,
        *,
        room_id: str,
        player_id: str,
        player_nickname: str,
        reason: str,
        roll: PendingDecision,
        notice_payload: dict[str, Any],
        cost: int,
        luck_remaining: int,
        decision_id: str | None = None,
    ) -> PendingDecision:
        """造一张幸运消费卡。

        🔴 它把**原来那条掷骰记录和已经掷出的结果一起收进 payload**：玩家的
        决定隔着一次 WS 往返，期间进程可能重启，那时只有数据库活着。生效那一步
        （`SettleHook.apply`）的输入因此被设计成"待决定项 + 结果通知"两样落库的
        东西——闭包跨不过这个等待。
        """
        return cls(
            decision_id=decision_id or str(uuid.uuid4()),
            kind=LUCK_SPEND_KIND,
            room_id=room_id,
            player_id=player_id,
            player_nickname=player_nickname,
            reason=reason,
            payload={
                "roll_kind": roll.kind,
                "roll_decision_id": roll.decision_id,
                "roll_reason": roll.reason,
                "roll_payload": dict(roll.payload),
                "notice": dict(notice_payload),
                "cost": cost,
                "luck_remaining": luck_remaining,
            },
        )

    def restore_roll(self) -> PendingDecision:
        """幸运卡 → 它挂着的那条掷骰记录（生效那一步要拿它当输入）。"""
        return PendingDecision(
            decision_id=str(self.payload["roll_decision_id"]),
            kind=str(self.payload["roll_kind"]),
            room_id=self.room_id,
            player_id=self.player_id,
            player_nickname=self.player_nickname,
            reason=str(self.payload.get("roll_reason") or ""),
            payload=dict(self.payload.get("roll_payload") or {}),
        )

    @property
    def cost(self) -> int:
        """花掉幸运把这次失败推成普通成功要付几点（= 出目 − 成功率）。"""
        return int(self.payload["cost"])

    @property
    def luck_remaining(self) -> int:
        """发这张卡时他手上还有多少幸运。"""
        return int(self.payload["luck_remaining"])

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
        target=pending.payload.get("target"),
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

    async def first(
        self, db: AsyncSession, room_id: str, kinds: frozenset[str] | set[str]
    ) -> PendingDecision | None:
        """队首那一项。

        🔴 `kinds` **必填**（exec/34）：队列里现在不止骰子。守秘人的待掷守卫
        问的是"还有没有骰子没掷"，如果连会合确认卡也算进去，那张卡一挂上，
        **整桌就说不了话了**——而它按设计可以一直挂着（没有超时自动确认）。
        加一种 kind 时，每个"逐个列出类别"的消费方都要回来看一眼。
        """
        row = await db.scalar(
            select(PendingDecisionRow)
            .where(PendingDecisionRow.room_id == room_id, PendingDecisionRow.kind.in_(kinds))
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

    async def list_all(
        self, db: AsyncSession, room_id: str, kinds: frozenset[str] | set[str] | None = None
    ) -> list[PendingDecision]:
        """队列里全部待决定项，按顺序。断线重连要靠它补发卡片——请求只在裁决
        那一刻推过一次，重连的人不补就永远看不到。"""
        stmt = select(PendingDecisionRow).where(PendingDecisionRow.room_id == room_id)
        if kinds is not None:
            stmt = stmt.where(PendingDecisionRow.kind.in_(kinds))
        rows = await db.scalars(stmt.order_by(PendingDecisionRow.seq))
        return [_to_decision(row) for row in rows]

    async def has(self, db: AsyncSession, room_id: str, kinds: frozenset[str] | set[str]) -> bool:
        """`kinds` 必填，理由同 `first`。"""
        row = await db.scalar(
            select(PendingDecisionRow.seq)
            .where(PendingDecisionRow.room_id == room_id, PendingDecisionRow.kind.in_(kinds))
            .limit(1)
        )
        return row is not None

    async def player_ids_of_kind(self, db: AsyncSession, room_id: str, kind: str) -> set[str]:
        """这个房间里，正挂着某一种决定的是哪些人。

        🔴 分组（`group_players`）每轮都要问「谁在等确认会合」——那是投递隔离的
        地基。`exec/34` 定的是**队列作唯一真相**，宁可多查一次库，也不在
        `keeper_state` 里留一份镜像：镜像必须随权威源重建，一处漏改就长期不一致。
        """
        rows = await db.scalars(
            select(PendingDecisionRow.player_id).where(
                PendingDecisionRow.room_id == room_id, PendingDecisionRow.kind == kind
            )
        )
        return set(rows)

    async def clear_room(self, db: AsyncSession, room_id: str) -> None:
        """清空一个房间的队列（对局结束/测试隔离用）。"""
        await db.execute(delete(PendingDecisionRow).where(PendingDecisionRow.room_id == room_id))
        await db.flush()


pending_decision_manager = PendingDecisionManager()
