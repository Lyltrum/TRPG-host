"""add characters occupation_id

职业改用 id 定位（exec/22）：职业名不唯一——规则表里有 6 组同名不同项的职业
（律师 ×2、私家侦探 ×2、工匠 ×2…），信用区间乃至技能点公式都不同，只存名字
时"玩家选的是哪一个"在落库那一刻就丢了。

Revision ID: 9c67731c71e7
Revises: cf102dc39e5f
Create Date: 2026-08-01 08:02:30.023673

"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9c67731c71e7"
down_revision: str | Sequence[str] | None = "cf102dc39e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    op.add_column("characters", sa.Column("occupation_id", sa.Integer(), nullable=True))
    _backfill()


def _backfill() -> None:
    """老数据按职业名回填 id。

    🔴 **歧义要记进日志，不静默**：同名职业本来就是这个 bug 的根源，回填时
    只能取第一个匹配——那正是我们要消灭的猜测。至少让它留下痕迹，之后能对着
    日志去核对那几张卡到底该是哪个变体。
    """
    from app.core.coc7_content import build_coc7_ruleset

    bind = op.get_bind()
    rows = list(
        bind.execute(sa.text("SELECT id, occupation FROM characters WHERE occupation IS NOT NULL"))
    )
    if not rows:
        return

    occupations = build_coc7_ruleset().occupations
    by_name: dict[str, list] = {}
    for occ in occupations:
        by_name.setdefault(occ.name, []).append(occ)

    filled = ambiguous = missing = 0
    for character_id, name in rows:
        matches = by_name.get(name or "", [])
        if not matches:
            missing += 1
            continue
        if len(matches) > 1:
            ambiguous += 1
            logger.warning(
                "occupation_backfill_ambiguous character=%s name=%s 取了 id=%s，候选 %s",
                character_id,
                name,
                matches[0].id,
                [o.id for o in matches],
            )
        bind.execute(
            sa.text("UPDATE characters SET occupation_id = :oid WHERE id = :cid"),
            {"oid": matches[0].id, "cid": character_id},
        )
        filled += 1
    logger.info(
        "occupation_backfill 完成：回填 %s 张，其中同名歧义 %s 张，职业名查不到 %s 张",
        filled,
        ambiguous,
        missing,
    )


def downgrade() -> None:
    op.drop_column("characters", "occupation_id")
