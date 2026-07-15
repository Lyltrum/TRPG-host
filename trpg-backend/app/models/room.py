"""Room + RoomPlayer ORM 模型。

- Room：房间主表
- RoomPlayer：房间内玩家成员表
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class Room(Base):
    __tablename__ = "rooms"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    room_code: Mapped[str] = mapped_column(
        String(6), unique=True, index=True, nullable=False
    )
    room_name: Mapped[str] = mapped_column(String(200), nullable=False)
    max_players: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(
        String(20), nullable=False, default="Lobby"
    )
    # 房主 playerId（由 RoomPlayer 的 id 填充，房间创建后通过 RoomPlayer.id 回写）
    host_player_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), nullable=True, default=None
    )
    module_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    players: Mapped[list["RoomPlayer"]] = relationship(
        back_populates="room", cascade="all, delete-orphan"
    )


class RoomPlayer(Base):
    __tablename__ = "room_players"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    room_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("rooms.id"), nullable=False
    )
    nickname: Mapped[str] = mapped_column(String(100), nullable=False)
    is_host: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ready: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_character: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    reconnect_token: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), default=lambda: str(uuid.uuid4()), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    room: Mapped["Room"] = relationship(back_populates="players")
