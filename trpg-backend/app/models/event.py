"""事件日志 ORM 模型（issue #77 §1，只增不改的 2 张表）。

- Event：房间内发生的所有事件的统一流水（叙事推送/玩家行动/未来的检定等），
  `GET /rooms/{roomId}/replay` 直接顺序读这张表——是本期唯一一条"服务端真的
  在写、也真的在读"的事件日志闭环（ws.py 在 narration.push / action.submit
  时插入行）。
- CheckResult：检定结果记录（技能检定/理智检定），本期 `check.roll`/
  `san.check.roll` 走 NOT_IMPLEMENTED 桩，不会真的写入这张表，只铺表结构。
- PendingDecisionRow：**待掷**检定队列（两段式玩家掷骰）。原先是进程内存 dict
  ——后端一重启队列就清空，而 `narrate` 有 pending 守卫，会一直回「请先完成
  待掷的检定」，**整局死锁**（exec/24 §8.1）。短模组一次跑完暴露不出来，
  长战役必然跨重启。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("rooms.id"), nullable=False
    )
    player_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("players.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class CheckResult(Base):
    __tablename__ = "check_results"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("rooms.id"), nullable=False
    )
    player_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("players.id"), nullable=False
    )
    character_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("characters.id"), nullable=True
    )
    check_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "skill" | "san"
    skill_or_stat: Mapped[str | None] = mapped_column(String(100), nullable=True)
    roll_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class PendingDecisionRow(Base):
    """一次**等某个玩家做决定**的请求；他没决定之前，这一轮不能往下走。

    与 `CheckResult` 的区别是时态：这张表装的是"已经判定要问、但玩家还没回答"
    的请求，答完就删；`CheckResult` 装的是掷完之后的留痕。

    🔴 **它曾经叫 `pending_checks`，只装掷骰**（`exec/34`）。那时"等玩家决定"
    确实只有掷骰一种，名字是对的；第二个实例（会合确认）出现时没有回头看，
    于是各写了一套——落库、重连补发、作废条件全部重来一遍，而
    `exec/23 #56`（"持久化必须配套重连补发"）那条教训因此要再学一遍。
    **一个概念被起了它某个实例的名字，症状不是 bug 是重复实现，不会有任何
    东西变红。**

    列只留**所有 kind 共有**的东西；每种 kind 自己的数据进 `payload`。
    这样加一种 kind **不用加迁移**，也不会出现"一种 kind 的字段占着公共表的列、
    别的挤在 JSON 里"那种不对称。

    排序用自增 `seq` 而不是 `created_at`：同一轮裁决可能一次挂起多个
    （技能 + 目击后的理智），毫秒级时间戳分不出先后，而队列顺序是有意义的
    （谁先答、`first()` 返回谁）。
    """

    __tablename__ = "pending_decisions"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: 不透明协议令牌（生产里是 uuid4，但它跨 WS 边界时就是一个字符串）。
    #: 故意不用 `Uuid` 列：钉成 UUID 在库这层买不到东西，却逼每个测试造 uuid。
    decision_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("rooms.id"), nullable=False, index=True
    )
    #: "skill" | "san"（掷骰）· 将来 "merge_confirm" · "luck_spend"。
    #: **扁平值域**：`settler_for(kind)` 按它分发，加一种就多一个认领者。
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    player_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("players.id"), nullable=False
    )
    player_nickname: Mapped[str] = mapped_column(String(100), nullable=False)
    #: 给玩家看的一句话（"搜索书房命中剧本检定点"）。所有 kind 都要。
    reason: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    #: 这一种 kind 自己的数据。掷骰放 skill / loss_on_* / reveals / opposed_*。
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
