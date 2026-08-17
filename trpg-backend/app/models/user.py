"""账号相关 ORM 模型（issue #77 §1，运行时状态库的一部分）。

`User`/`UserSession` 承接 issue #58 之前用内存字典（`_users`/`_accounts`/
`_tokens`）实现的账号+会话逻辑；`UserCharacterTemplate` 是本期新增的"我的
常用角色卡库"（issue 决策 5），本期只铺表与接口，不实现真实读写（详见
service/character.py）。
"""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class User(Base):
    """账号。"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    account: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    nickname: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class UserSession(Base):
    """登录会话：`Authorization: Bearer <token>` 里的 token 就是这里的 `token` 列。

    本期不做过期/续期（跟原来的内存 stub 行为一致），`token` 直接唯一索引，
    退出登录时整行删除。
    """

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id"), nullable=False
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class UserCharacterTemplate(Base):
    """玩家的"我的常用角色卡"库（issue 决策 5）。

    `system_id` 约束死了这张卡只能用于同一个规则系统（COC7 的卡不能拿去玩
    DND5e）；只存建卡态字段（放在 `data` 里），不带任何单局才有的状态
    （HP/理智/疯狂），复用时天然不会把上一局的状态带进新局。本期只铺表与
    接口，不实现真实读写（service 层直接返回 NOT_IMPLEMENTED）。
    """

    __tablename__ = "user_character_templates"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id"), nullable=False
    )
    system_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("game_systems.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class LlmDailyUsage(Base):
    """一个账号在一个 UTC 日历日里发了多少次 LLM 调用。

    ## 为什么按「调用次数」而不是「回合数」或「token 数」

    - **回合数**不反映成本：一个守秘人回合是 3–8 次调用（裁决 + 叙事 + 可能的
      摘要 + 结算那一拍），按回合计会把差着一倍的两轮算成一样。
    - **token 数**最准，但它只有在**调用返回之后**才知道，而闸门必须在调用
      **之前**关上——那正是要防的那一笔钱。次数是唯一"事前可判"的量。

    ## 为什么带 `day` 而不是滚动窗口

    滚动窗口要留每次调用的时间戳（一天几千行）并按时间聚合；日历日只要一行、
    一个整数。配额的用途是"别让一个账号把当天额度烧光"，不是精确限速。

    🔴 **UTC 日，不是本地日。** 服务器时区变了、跨夏令时了，本地日会让某一天
    变成 23 或 25 小时，配额跟着缩水或翻倍。同 `UtcDatetime` 那次的判据。

    唯一约束 `(user_id, day)` 是**必须的**：记账走 UPDATE→（没命中再）INSERT，
    没有唯一约束的话两个并发请求会各插一行，此后每次 UPDATE 只命中其中一行，
    计数**永远差一半**且不会有任何东西变红。
    """

    __tablename__ = "llm_daily_usage"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_llm_usage_user_day"),)

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("users.id"), nullable=False, index=True
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
