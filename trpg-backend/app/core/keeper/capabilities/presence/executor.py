"""presence 的执行层：把"交代过了"记下来。

没有裁决字段——**这一片不需要模型说什么**，它需要模型**写**什么（那是叙事）。
执行钩子在这里只做记账：本轮局面块已经把人摆到它眼前了，那就算交代过。

🔴 因此它是**概率性改进**（`exec/20`）：触发条件由代码判（谁没被交代过），
执行方式是请模型在叙事里圆一句。结构性的那一半在别处且是硬的——暂离的人
**根本不在** `players` 名单里，模型想提他也提不了。
"""

from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import select

from app.core.keeper.capabilities.presence.state import (
    ANNOUNCED_ARRIVALS_KEY,
    PENDING_DEPARTURES_KEY,
    load_announced_arrivals,
    load_pending_departures,
    unannounced_arrivals,
)
from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.runtime.deps import KeeperDeps, record_event
from app.models.room import Player, Room


async def mark_presence_announced(
    deps: KeeperDeps, _decision: BaseModel, _facts: TurnFacts
) -> tuple[list[str], list[str]]:
    """本轮跑完 = 局面块已经摆到模型眼前 ⇒ 记成已交代。

    🔴 **执行报告恒为空**，哪怕真的有人来了或走了。第一版每轮都往报告里塞
    一行「新到场：阿福」，当场打红了九条**别的**能力的既有测试——它们断言的是
    「本轮报告有几条」。那不只是测试脆：执行报告的语义是**世界变了什么**
    （喂给叙事阶段的事实），而"我已经提醒过他了"是记账，不是世界变化；
    况且局面块已经把同一件事摆到模型眼前了，报告里再说一遍是重复。

    ⚠️ 记账与"模型真的写了"之间有缝：它可能拿到了提示却没写。**不重试**——
    重试意味着每一轮都再提醒一次，而那会让守秘人反复介绍同一个人登场，
    比漏一次更糟（同 `已触发理智检定点` 那笔记账存在的理由）。
    """
    async with deps.write_lock, deps.session_factory() as db:
        room = await db.get(Room, deps.room_id)
        if room is None:
            return [], ["登场/离场记账未执行：房间不存在"]
        state = dict(room.keeper_state or {})

        # 在场名单在这里重算而不是从 deps 拿：执行钩子没有 players 参数，
        # 而这份名单的口径必须跟 agent 那边一致（暂离的人不在场）。
        result = await db.execute(select(Player).where(Player.room_id == deps.room_id))
        present = tuple((p.id, p.nickname) for p in result.scalars() if not p.away)

        arrivals = unannounced_arrivals(state, present)
        departures = load_pending_departures(state)
        if not arrivals and not departures:
            return [], []

        announced = load_announced_arrivals(state)
        announced.extend(pid for pid, _name in arrivals if pid not in announced)
        state[ANNOUNCED_ARRIVALS_KEY] = ", ".join(announced)
        state[PENDING_DEPARTURES_KEY] = ""
        room.keeper_state = state
        await record_event(
            db,
            deps,
            "keeper.presence",
            {
                "arrived": [name for _pid, name in arrivals],
                "left": [name for _pid, name in departures],
            },
        )

    # 留痕走 `keeper.presence` 事件（上面那行），不走执行报告——理由见 docstring。
    return [], []
