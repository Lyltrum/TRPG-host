"""add rooms.keeper_state

keeper agent（feat/keeper-agent 实验分支）的世界状态笔记：AI 主持人在推进
剧情时通过 update_state 工具记录的键值对（当前场景、已揭示线索、NPC 状态、
时间压力进度等），每轮生成前整体注入 prompt。JSON 列而不是独立表——这是
agent 的自由笔记本，形状由 agent 自己决定，后端不解释其内容。

Revision ID: e7a1c40b9f21
Revises: c288fe8f2cdd
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7a1c40b9f21"
down_revision: str | Sequence[str] | None = "c288fe8f2cdd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("rooms", sa.Column("keeper_state", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("rooms", "keeper_state")
