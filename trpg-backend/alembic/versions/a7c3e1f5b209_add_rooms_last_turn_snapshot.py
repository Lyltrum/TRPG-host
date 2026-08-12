"""rooms.last_turn_snapshot —— 玩家纠错通道的回滚点

存「上一轮开始之前」的世界指针（keeper_state）和那一轮的原话。
玩家点「不是这个意思」时，指针回滚到这里再重裁一次。

🔴 只存指针，不存 HP/线索/骰子——那些是已发生的事实，纠错不该撤销它们
（骰子能撤就等于能刷）。

Revision ID: a7c3e1f5b209
Revises: f1a2b3c4d5e6
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7c3e1f5b209"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("rooms", sa.Column("last_turn_snapshot", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("rooms", "last_turn_snapshot")
