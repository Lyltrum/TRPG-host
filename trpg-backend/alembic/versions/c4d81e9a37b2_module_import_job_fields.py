"""module import job: stage, report counters, retry chain

模组导入第 5 步（`exec/29 §7.2`）：把骨架期占位的 `module_import_jobs` 补成
真能跑的 job。

🔴 **报告字段全是显式整数列，没有自由 JSON。** job 的字段是唯一跨到前端的
东西，而本功能的第一性约束是「人类不许看见模组内容」——一个 `stats: JSON`
能装下任何东西，包括模组正文。失败原因也只留封闭集合里的**类别词**
（`job_state.FAILURE_KINDS`），不是错误原文（原文里带着实体 id 和正文片段）。

`source_path` 指向服务器上保存的用户上传件（第三方正文），**内部字段，不进
任何 DTO**。

Revision ID: c4d81e9a37b2
Revises: b3f7c21d84ae
Create Date: 2026-08-04

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d81e9a37b2"
down_revision: str | Sequence[str] | None = "b3f7c21d84ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 计数列：默认 0，非空。分开列而不是一个 JSON，见模块文档。
_COUNTERS = (
    "page_count",
    "image_count",
    "char_count",
    "item_count",
    "node_count",
    "npc_count",
    "ending_count",
    "agenda_count",
    "hard_failure_count",
)


def upgrade() -> None:
    """Upgrade schema."""
    # SQLite 不支持 ALTER 加外键，要走 batch 模式重建表。
    with op.batch_alter_table("module_import_jobs") as batch:
        batch.add_column(sa.Column("owner_user_id", sa.Uuid(as_uuid=False), nullable=True))
        batch.add_column(
            sa.Column("stage", sa.String(length=20), nullable=False, server_default="received")
        )
        batch.add_column(sa.Column("source_sha256", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("source_path", sa.Text(), nullable=True))
        batch.add_column(sa.Column("failure_kinds", sa.JSON(), nullable=True))
        for name in _COUNTERS:
            batch.add_column(sa.Column(name, sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("retried_from_job_id", sa.Uuid(as_uuid=False), nullable=True))
        batch.add_column(sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_module_import_jobs_owner_user_id_users", "users", ["owner_user_id"], ["id"]
        )
        batch.create_foreign_key(
            "fk_module_import_jobs_retried_from",
            "module_import_jobs",
            ["retried_from_job_id"],
            ["id"],
        )
    op.create_index("ix_module_import_jobs_source_sha256", "module_import_jobs", ["source_sha256"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_module_import_jobs_source_sha256", table_name="module_import_jobs")
    with op.batch_alter_table("module_import_jobs") as batch:
        batch.drop_constraint("fk_module_import_jobs_retried_from", type_="foreignkey")
        batch.drop_constraint("fk_module_import_jobs_owner_user_id_users", type_="foreignkey")
        batch.drop_column("finished_at")
        batch.drop_column("retried_from_job_id")
        for name in _COUNTERS:
            batch.drop_column(name)
        batch.drop_column("failure_kinds")
        batch.drop_column("source_path")
        batch.drop_column("source_sha256")
        batch.drop_column("stage")
        batch.drop_column("owner_user_id")
