"""imported modules table and scenario owner

模组导入第 1 步（`exec/29`）：模组从「编译期常量」变成「运行时数据」。

- `imported_modules`：导入模组的 structured 正文。**内置五个模组不落库**
  （「随发版进来的东西不进数据库」）。
- `scenarios.owner_user_id`：导入者。只决定「谁能拿它开新局」，不决定「谁能玩」
  ——续玩看 `rooms.scenario_id`。内置模组无主，保持 NULL。

Revision ID: b3f7c21d84ae
Revises: de3225b1cd99
Create Date: 2026-08-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f7c21d84ae"
down_revision: str | Sequence[str] | None = "de3225b1cd99"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "imported_modules",
        sa.Column("scenario_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("structured", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["scenario_id"], ["scenarios.id"]),
        sa.PrimaryKeyConstraint("scenario_id"),
    )
    # SQLite 不支持 ALTER 加外键，要走 batch 模式重建表
    # （先例：`#106` 那次加唯一约束踩过）。
    with op.batch_alter_table("scenarios") as batch:
        batch.add_column(sa.Column("owner_user_id", sa.Uuid(as_uuid=False), nullable=True))
        batch.create_foreign_key(
            "fk_scenarios_owner_user_id_users", "users", ["owner_user_id"], ["id"]
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("scenarios") as batch:
        batch.drop_constraint("fk_scenarios_owner_user_id_users", type_="foreignkey")
        batch.drop_column("owner_user_id")
    op.drop_table("imported_modules")
