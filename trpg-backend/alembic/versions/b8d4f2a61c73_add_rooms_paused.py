"""rooms.paused —— 大家在休息（`exec/35`）

聚会游戏的物理现实：有人上厕所、点外卖、接电话。此前只能干晾着，
而世界心跳还会自己往前推。

Revision ID: b8d4f2a61c73
Revises: a7c3e1f5b209
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8d4f2a61c73"
down_revision: str | Sequence[str] | None = "a7c3e1f5b209"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rooms",
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("rooms", "paused")
