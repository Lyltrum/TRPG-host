"""timestamps not null: 让迁移建出来的表跟模型说的一致

Revision ID: c3f81b0d7e42
Revises: b8d02f6a1c93
Create Date: 2026-08-17 12:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3f81b0d7e42"
down_revision: str | Sequence[str] | None = "b8d02f6a1c93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: (表, 列)——模型里是 `Mapped[datetime]`（非 Optional，即 NOT NULL），
#: 而建表迁移写成了 `nullable=True`。
_DRIFTED: tuple[tuple[str, str], ...] = (
    ("llm_daily_usage", "created_at"),
    ("llm_daily_usage", "updated_at"),
    ("pending_decisions", "created_at"),
)


def upgrade() -> None:
    """把三个时间戳列改回 NOT NULL，跟模型对齐。

    ## 🔴 这是 `alembic check` 抓到的，不是人看出来的

    测试建表走 `create_all`（读模型），生产建表走迁移——**两条路建出来的表
    不一样，而两边都不会变红**：测试里这些列是 NOT NULL，生产库里可以写进
    NULL，直到某段代码假设它非空才出事。

    同项目反复吃亏的「一份知识在两个地方各写一遍」。这次的守门人是 CI 里的
    `alembic check`，它比对模型与迁移后的库，有差异就非零退出。

    ## 为什么不直接改那两个建表迁移

    `b8d02f6a1c93` 已经推出去、也已经在开发库上跑过了。**迁移是历史，不是
    可编辑的配置**——改它会让"已经升过的库"和"从头升的库"走出两种结构。
    修正只能往前加一条。
    """
    for table, column in _DRIFTED:
        # 先填空值再收紧：已有行可能是 NULL（正是这个 bug 允许的），
        # 直接 SET NOT NULL 会失败。用当前时间兜底——这些列本来就只是留痕，
        # 填一个"我们发现它是空的那一刻"比让迁移炸掉合理。
        op.execute(
            sa.text(
                f"UPDATE {table} SET {column} = CURRENT_TIMESTAMP WHERE {column} IS NULL"  # noqa: S608
            )
        )
        # batch 模式：SQLite 不支持 ALTER COLUMN，要靠重建表实现。
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(column, existing_type=sa.DateTime(timezone=True), nullable=False)


def downgrade() -> None:
    for table, column in _DRIFTED:
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(column, existing_type=sa.DateTime(timezone=True), nullable=True)
