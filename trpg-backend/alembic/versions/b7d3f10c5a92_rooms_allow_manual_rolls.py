"""rooms.allow_manual_rolls

Revision ID: b7d3f10c5a92
Revises: a4c7e9b21d80
Create Date: 2026-08-24 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7d3f10c5a92"
down_revision: str | Sequence[str] | None = "a4c7e9b21d80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """「骰子在桌上」的房间级开关（`exec/46` B5）。

    默认 `False` = 现状逐字不变：不开它的房间，掷骰这条路一个字节都没变。

    `server_default` 是给**已有的行**用的——加列时那些行需要一个值，而
    模型层的 `default=False` 只作用于新插入的对象（Python 侧）。老库升上来
    之后每一行都得是 False，否则读出来是 NULL、而列声明是 `nullable=False`。
    """
    with op.batch_alter_table("rooms") as batch:
        batch.add_column(
            sa.Column(
                "allow_manual_rolls",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("rooms") as batch:
        batch.drop_column("allow_manual_rolls")
