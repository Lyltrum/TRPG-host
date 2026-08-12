"""players.away —— 中途离开（暂离）

🔴 **不复用 `left_at`**：那一列已经被 WS 断开占用了
（`set_player_connected` 里断线就写它），而**掉线不等于离场**——网卡抖一下
角色就该从剧情里消失，那是「一份数据扮演两个角色必出结构性 bug」。

Revision ID: c9e5a3b71d84
Revises: b8d4f2a61c73
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9e5a3b71d84"
down_revision: str | Sequence[str] | None = "b8d4f2a61c73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "players",
        sa.Column("away", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("players", "away")
