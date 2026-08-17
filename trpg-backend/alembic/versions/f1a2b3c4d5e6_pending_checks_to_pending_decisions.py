"""pending_checks → pending_decisions（exec/34 第 1 步）

把「待掷检定队列」收成「待玩家决定队列」：列只留所有 kind 共有的，
每种 kind 自己的数据进 payload（JSON）。**加一种 kind 不用再加迁移。**

行为零变化：老数据里的骰子字段原样搬进 payload。

Revision ID: f1a2b3c4d5e6
Revises: c4d81e9a37b2
"""

import json
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
    #
    # 🔴 **在 Python 侧拼 payload，不用数据库的 JSON 函数。** 第一版写的是
    # SQLite 的 `json_object()` / `json()`，在 Postgres 上直接炸
    # （`UndefinedFunctionError`——PG 那边叫 `json_build_object`，签名还不一样）。
    # 迁移不经过测试（测试建表走 `create_all`），所以这个错在 SQLite 上跑了
    # 十几次都没人发现，直到第一次真的往 Postgres 上迁。
    # 这里改成方言无关：读出来、在 Python 里拼、参数化写回去，两种库同一条路。
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT check_request_id, room_id, kind, player_id, player_nickname, reason,
                   skill, loss_on_success, loss_on_failure, reveals,
                   opposed_opponent, opposed_value, created_at
            FROM pending_checks
            """
        )
    ).mappings()

    for row in rows:
        raw_reveals = row["reveals"]
        # 老表里 `reveals` 是 JSON 列：SQLite 给回字符串，PG 给回已解析的对象。
        # 两种都要接住——**不要 `?? []` 式的静默兜底**，只在真的没有时才给空表。
        if raw_reveals is None:
            reveals: object = []
        elif isinstance(raw_reveals, str):
            reveals = json.loads(raw_reveals)
        else:
            reveals = raw_reveals

        bind.execute(
            sa.text(
                """
                INSERT INTO pending_decisions
                    (decision_id, room_id, kind, player_id, player_nickname, reason,
                     payload, created_at)
                VALUES
                    (:decision_id, :room_id, :kind, :player_id, :player_nickname, :reason,
                     :payload, :created_at)
                """
            ).bindparams(sa.bindparam("payload", type_=sa.JSON())),
            {
                "decision_id": row["check_request_id"],
                "room_id": row["room_id"],
                "kind": row["kind"],
                "player_id": row["player_id"],
                "player_nickname": row["player_nickname"],
                "reason": row["reason"],
                "payload": {
                    "skill": row["skill"],
                    "loss_on_success": row["loss_on_success"],
                    "loss_on_failure": row["loss_on_failure"],
                    "reveals": reveals,
                    "opposed_opponent": row["opposed_opponent"],
                    "opposed_value": row["opposed_value"],
                },
                "created_at": row["created_at"],
            },
        )

    op.drop_table("pending_checks")


def downgrade() -> None:
    raise NotImplementedError("不提供降级：payload → 列的反向拆解没有使用场景")
