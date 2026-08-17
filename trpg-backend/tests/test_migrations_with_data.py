"""带数据的迁移：**空库跑通不算验证过**。

## 🔴 为什么必须造数据

两条数据搬迁迁移都写成「先查出要处理的行，为空就整段跳过」。于是在空库上
`alembic upgrade head` 会**从它们旁边绕过去**——十几次绿灯，一行都没执行。

2026-08-17 第一次真的喂给它们数据（迁 Postgres 时造的样本），当场炸出三个
bug，其中一个是**引用了一个根本不存在的列**（`events.character_id`，那一列
在 `check_results` 上）。那个 bug 跟 Postgres 无关——它在 SQLite 上一样会炸，
只是从来没有人给过它重复数据。

「造的样本没走到被测分支 = 没测」，这个文件就是那条判据的落地。

## 为什么用 subprocess 调 alembic CLI

跟人手跑的是同一条路径（`env.py`、`DATABASE_URL`、真实的迁移链），不绕过
任何一层。用编程 API 的话要自己处理 `get_settings()` 的 lru_cache 和
`asyncio.run` 与 pytest 事件循环的关系——那等于测一条真实使用中不存在的路。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]

#: 建 pending_decisions 的那条（`exec/34` 第 1 步）
_PENDING_MIGRATION = "f1a2b3c4d5e6"
#: 它的前一版
_BEFORE_PENDING = "c4d81e9a37b2"

#: 给 characters 加唯一约束、顺带清重复卡的那条
_DEDUP_MIGRATION = "e2b91f4c7a56"
_BEFORE_DEDUP = "c9e5a3b71d84"


def _alembic(db_path: Path, revision: str) -> None:
    """把 `db_path` 迁到指定 revision。失败时把 alembic 的输出原样抛出来。"""
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=_BACKEND,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"alembic upgrade {revision} 失败：\n{result.stdout}\n{result.stderr}")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    # 🔴 绝对路径。相对路径下 sqlite 会「新建一个空库」而不是报错。
    return tmp_path / "migrate.db"


def _seed_room_and_players(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO rooms (id, room_code, room_name, max_players, phase,"
        " discovered_scene_ids, created_at, updated_at)"
        " VALUES ('r1','MIG001','迁移样本',6,'InGame','[]','2026-08-01','2026-08-01')"
    )
    for pid, nick in (("p1", "凌铭辉"), ("p2", "阿贵")):
        conn.execute(
            "INSERT INTO players (id, room_id, is_ai, nickname, is_host, ready,"
            " has_character, reconnect_token, connected, joined_at)"
            " VALUES (?, 'r1', 0, ?, 0, 1, 1, ?, 1, '2026-08-01')",
            (pid, nick, f"rt-{pid}"),
        )


def test_the_pending_queue_migration_moves_rows_instead_of_dropping_them(db: Path) -> None:
    """队列里挂着的待掷检定必须搬过去。

    丢了就是「玩家等一张永远不来的卡片」——那正是当初把队列落库要解决的死锁。
    """
    _alembic(db, _BEFORE_PENDING)

    conn = sqlite3.connect(db)
    _seed_room_and_players(conn)
    conn.executemany(
        "INSERT INTO pending_checks (check_request_id, room_id, kind, player_id,"
        " player_nickname, skill, loss_on_success, loss_on_failure, reason, reveals,"
        " opposed_opponent, opposed_value, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            # 满字段的一行
            (
                "chk-1",
                "r1",
                "san",
                "p1",
                "凌铭辉",
                "skill-spot-hidden",
                "1",
                "1d6",
                "看见了那个东西",
                '["fact-3","fact-9"]',
                "npc-warden",
                65,
                "2026-08-17 03:00:00",
            ),
            # 可空列全空的一行——**两条分支都要走到**，否则又是"没测"
            (
                "chk-2",
                "r1",
                "skill",
                "p2",
                "阿贵",
                None,
                "0",
                "0",
                "撬锁",
                "[]",
                None,
                None,
                "2026-08-17 03:01:00",
            ),
        ],
    )
    conn.commit()
    conn.close()

    _alembic(db, _PENDING_MIGRATION)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = {
        r["decision_id"]: r
        for r in conn.execute("SELECT decision_id, kind, payload FROM pending_decisions")
    }
    assert set(rows) == {"chk-1", "chk-2"}, "有待掷记录在迁移中丢了"

    full = json.loads(rows["chk-1"]["payload"])
    # 🔴 `reveals` 必须是**数组**，不是那段 JSON 的字符串形式。第一版用
    # SQLite 的 `json()` 达成这一点，换 Postgres 就没有这个函数了。
    assert full["reveals"] == ["fact-3", "fact-9"]
    assert isinstance(full["reveals"], list)
    assert full["opposed_value"] == 65, "数字不该在搬迁中变成字符串"
    assert full["skill"] == "skill-spot-hidden"

    empty = json.loads(rows["chk-2"]["payload"])
    assert empty["reveals"] == []
    assert empty["skill"] is None
    assert empty["opposed_value"] is None
    conn.close()


def test_the_dedup_migration_keeps_the_newest_card_and_clears_dangling_pointers(
    db: Path,
) -> None:
    """同一个 (房间, 玩家) 只留 `updated_at` 最新那张，指向被删卡的引用要置空。

    🔴 这条会抓住「引用了不存在的列」那类错——而它**只在真的有重复卡时**
    才执行得到。
    """
    _alembic(db, _BEFORE_DEDUP)

    conn = sqlite3.connect(db)
    _seed_room_and_players(conn)
    for cid, pid, name, updated in (
        ("c1", "p1", "旧卡A", "2026-08-01 00:00:00"),
        ("c2", "p1", "旧卡B", "2026-08-02 00:00:00"),
        ("c3", "p1", "最新卡", "2026-08-03 00:00:00"),
        ("c4", "p2", "阿贵的卡", "2026-08-01 00:00:00"),  # 独苗，不该被碰
    ):
        conn.execute(
            "INSERT INTO characters (id, room_id, player_id, status, name, background,"
            " notes, created_at, updated_at) VALUES (?,'r1',?,'complete',?,'','',"
            " '2026-08-01', ?)",
            (cid, pid, name, updated),
        )
    # 一条指向「即将被删的旧卡」的检定记录：它必须被置空，而不是让删除失败
    conn.execute(
        "INSERT INTO check_results (id, room_id, player_id, character_id, check_type,"
        " created_at) VALUES ('k1','r1','p1','c1','skill','2026-08-01')"
    )
    conn.commit()
    conn.close()

    _alembic(db, _DEDUP_MIGRATION)

    conn = sqlite3.connect(db)
    survivors = sorted(r[0] for r in conn.execute("SELECT name FROM characters"))
    assert set(survivors) == {"最新卡", "阿贵的卡"}, f"留下的不对：{survivors}"
    assert len(survivors) == 2, f"重复卡没清干净：{survivors}"

    dangling = conn.execute("SELECT character_id FROM check_results WHERE id='k1'").fetchone()
    assert dangling[0] is None, "指向被删角色卡的引用没有被置空——悬空指针比没有指针更坏"
    conn.close()
