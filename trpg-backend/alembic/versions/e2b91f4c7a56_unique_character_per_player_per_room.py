"""unique character per player per room

Revision ID: e2b91f4c7a56
Revises: c9e5a3b71d84
Create Date: 2026-08-13 22:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2b91f4c7a56"
down_revision: str | Sequence[str] | None = "c9e5a3b71d84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """给 characters 加 (room_id, player_id) 唯一约束。

    「一个玩家在一个房间只有一张角色卡」此前只是**惯例**：`quick_build` 复用
    已有那行，`create_character_draft` 却每次新建，于是连点几次「用我的常用卡」
    就留下几张。而"哪张算数"两条读路径不一致——重连取第一行、队伍面板与守秘人
    取最后一行，两处都没有 ORDER BY。service 层已经改成复用，但**不变式得由
    数据库保证**：只靠先查再插是 check-then-act，两个并发请求会各插一行。

    batch 模式的理由同 `1a02058345ee`：SQLite 不支持 ALTER TABLE ADD CONSTRAINT。

    先清脏数据：同一 (room_id, player_id) 只留 `updated_at` 最新的那行——那正是
    队伍面板和守秘人当前认的那张（dict 覆盖，后写的胜出），删旧的不会改变现在
    桌上生效的卡。`check_results.character_id` 指向被删行的记录一并置空（它可空）。

    🔴 **这段清理代码原先写的是 `events.character_id`，而那一列根本不存在**
    （`character_id` 在 `check_results` 上）。它没被发现是因为**只有存在重复卡
    时才会执行**——空的 `stale` 直接跳过整段，于是开发库和测试都从它旁边绕了
    过去。第一次真的喂给它重复数据（迁 Postgres 时造的样本）就当场炸了。
    「造的样本没走到被测分支 = 没测」的又一个实例。
    """
    # 🔴 用窗口函数挑「每组留哪一行」，不用 `GROUP BY` + 裸 `SELECT id`。
    # 原写法在 SQLite 上跑得通（它对 GROUP BY 外的列很宽松，随便挑一行给你），
    # 在 Postgres 上直接炸：`column "c.id" must appear in the GROUP BY clause`。
    # 而且那份宽松本身就是个坑——`updated_at` 撞平的时候「随便挑一行」意味着
    # 保留哪张卡是**不确定的**。`ROW_NUMBER` 顺带把这一点定死了（撞平时按 id
    # 取大的），两种库拿到同一个结果。
    conn = op.get_bind()
    stale = (
        conn.execute(
            sa.text(
                """
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY room_id, player_id
                           ORDER BY updated_at DESC, id DESC
                       ) AS rn
                FROM characters
            ) ranked
            WHERE rn > 1
            """
            )
        )
        .scalars()
        .all()
    )
    if stale:
        conn.execute(
            sa.text(
                "UPDATE check_results SET character_id = NULL WHERE character_id IN :ids"
            ).bindparams(sa.bindparam("ids", value=tuple(stale), expanding=True))
        )
        conn.execute(
            sa.text("DELETE FROM characters WHERE id IN :ids").bindparams(
                sa.bindparam("ids", value=tuple(stale), expanding=True)
            )
        )

    with op.batch_alter_table("characters") as batch_op:
        batch_op.create_unique_constraint("uq_characters_room_player", ["room_id", "player_id"])


def downgrade() -> None:
    with op.batch_alter_table("characters") as batch_op:
        batch_op.drop_constraint("uq_characters_room_player", type_="unique")
