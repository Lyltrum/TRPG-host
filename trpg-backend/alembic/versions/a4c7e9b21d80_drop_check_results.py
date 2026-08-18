"""drop check_results: 一张从来没人写过的表

Revision ID: a4c7e9b21d80
Revises: c3f81b0d7e42
Create Date: 2026-08-18 16:40:00.000000

它是 issue #77 §1 里"先铺表结构"的产物——当时 `check.roll`/`san.check.roll`
还是 NOT_IMPLEMENTED 桩，模型文档里就写着「不会真的写入这张表」。后来真正的
检定留痕走了 `events`（`keeper.check` / `keeper.san`），前端的掷骰卡片与重连
回放都读那一条路（`formatCheckLine` 实时与回放共用），这张表再没人管过。

删之前查过两头：全库 `select count(*)` 为 0，代码里既没有 `CheckResult(...)`
构造也没有任何 select。同族的还有 `deps.check_results`（同日删，`52d1d4b`）。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4c7e9b21d80"
down_revision: str | Sequence[str] | None = "c3f81b0d7e42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("check_results")


def downgrade() -> None:
    # 原样重建（含外键与可空性），好让降级之后的库跟升级之前逐列一致。
    op.create_table(
        "check_results",
        sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column("room_id", sa.Uuid(as_uuid=False), sa.ForeignKey("rooms.id"), nullable=False),
        sa.Column("player_id", sa.Uuid(as_uuid=False), sa.ForeignKey("players.id"), nullable=False),
        sa.Column(
            "character_id", sa.Uuid(as_uuid=False), sa.ForeignKey("characters.id"), nullable=True
        ),
        sa.Column("check_type", sa.String(20), nullable=False),
        sa.Column("skill_or_stat", sa.String(100), nullable=True),
        sa.Column("roll_value", sa.Integer(), nullable=True),
        sa.Column("target_value", sa.Integer(), nullable=True),
        sa.Column("result", sa.String(20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
