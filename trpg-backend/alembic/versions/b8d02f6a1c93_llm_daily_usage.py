"""llm daily usage quota

Revision ID: b8d02f6a1c93
Revises: e2b91f4c7a56
Create Date: 2026-08-17 02:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8d02f6a1c93"
down_revision: str | Sequence[str] | None = "e2b91f4c7a56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """账号级 LLM 日调用配额的记账表。

    唯一约束 `(user_id, day)` 不是"顺手加的"：记账走 `UPDATE ... calls + 1`，
    没命中再 `INSERT`。没有这条约束，两个并发回合会各插一行，此后每次 UPDATE
    只命中其中一行，计数**永远差一半**——而且不会有任何东西变红。
    """
    op.create_table(
        "llm_daily_usage",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "day", name="uq_llm_usage_user_day"),
    )
    op.create_index(
        op.f("ix_llm_daily_usage_user_id"), "llm_daily_usage", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_llm_daily_usage_user_id"), table_name="llm_daily_usage")
    op.drop_table("llm_daily_usage")
