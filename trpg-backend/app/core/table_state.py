"""桌子开着没有：两种「停」的唯一判据（`exec/46` B3）。

放在 `app/core` 而不是 `app/service`，是因为**它是领域规则不是数据访问**：
一个纯函数、只读 `Room` 的两个字段。放进 service 层之后 `runtime.heartbeat`
要用它就得反向依赖 service——`test_layers_do_not_invert` 当场把这条抓了出来，
而那份豁免清单的注释明写着「只能变短」。

场次记录的读写（开一场、结一场、数几场）留在 `service/table_session.py`，
那些是真的要碰数据库。
"""

from __future__ import annotations

from app.models.room import Room

#: 大厅级阶段：今晚散会了，但这一局没结束。`InGame` ↔ `Adjourned` 可逆。
#:
#: 🔴 命名用 `Adjourned`（散会）不用 `Paused`：跟 `room.paused`（临时休息）
#: 撞名会让下一个人以为它们是一回事，而它们是两档粒度。
PHASE_ADJOURNED = "Adjourned"

#: `RoomSession.status` 的两个取值。
SESSION_ACTIVE = "active"
SESSION_ENDED = "ended"


def table_is_open(room: Room) -> bool:
    """现在能不能开新的一轮。

    🔴 **两种停都在这里**：短暂休息（`room.paused`，`exec/35`）与今晚散会
    （`phase == Adjourned`，`exec/46` B3）。它们的表现完全一样（不开新的一轮、
    心跳不推进、讨论区照常），区别只在谁能按、按下去生成什么。

    加第三种停时**改这一处**，别再去各个调用点补 `or`——项目判据：逐个列出的
    断言，加一项就漏一项。
    """
    return not room.paused and room.phase != PHASE_ADJOURNED
