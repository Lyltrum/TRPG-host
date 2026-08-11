"""pending_checks → pending_decisions（exec/34 第 1 步）

把「待掷检定队列」收成「待玩家决定队列」：列只留所有 kind 共有的，
每种 kind 自己的数据进 payload（JSON）。**加一种 kind 不用再加迁移。**

行为零变化：老数据里的骰子字段原样搬进 payload。

Revision ID: f1a2b3c4d5e6
Revises: c4d81e9a37b2
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "c4d81e9a37b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_decisions",
        sa.Column("seq", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("decision_id", sa.String(length=64), nullable=False),
        sa.Column("room_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("player_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("player_nickname", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"]),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"]),
        sa.PrimaryKeyConstraint("seq"),
    )
    op.create_index(
        op.f("ix_pending_decisions_decision_id"),
        "pending_decisions",
        ["decision_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_pending_decisions_room_id"), "pending_decisions", ["room_id"], unique=False
    )

    # 🔴 搬数据而不是丢：队列里可能正挂着某个房间等玩家掷的骰子，
    # 丢了就是「玩家等一张永远不来的卡片」——那正是当初落库要解决的死锁。
    # `reveals` 在老表里已经是 JSON 列，这里直接嵌进 payload。
    op.execute(
        sa.text(
            """
            INSERT INTO pending_decisions
                (decision_id, room_id, kind, player_id, player_nickname, reason,
                 payload, created_at)
            SELECT check_request_id, room_id, kind, player_id, player_nickname, reason,
                   json_object(
                       'skill', skill,
                       'loss_on_success', loss_on_success,
                       'loss_on_failure', loss_on_failure,
                       'reveals', json(COALESCE(reveals, '[]')),
                       'opposed_opponent', opposed_opponent,
                       'opposed_value', opposed_value
                   ),
                   created_at
            FROM pending_checks
            """
        )
    )
    op.drop_table("pending_checks")


def downgrade() -> None:
    raise NotImplementedError("不提供降级：payload → 列的反向拆解没有使用场景")
