"""跑完一拍之后，WS 那条 session 上读到的必须是**新**状态。

## 🔴 起点是 2026-08-18 的两局真机

第二局打到剧本预设结局，库里 `对局阶段=finished`、`结局=truth`，而整局
**20 条 `keeper.phase` 推送一条 `finished` 都没有** —— 玩家屏幕永远停在
「调查中」。换一条连接重新 `room.join` 立刻拿到 `finished`：**值是对的，
坏的是那次读**。

机制见 `_fresh_room` 的 docstring。这个文件守两头：

- `keeper.phase` 那条**不会自己纠正**（finished 那拍后面没有下一拍了）；
- 投递受众按 `keeper_state` 算，读旧的 = 这一拍刚移动过的人收错卡片。

两条用例都把真实形状摆出来：**同一个 session 先读一次 Room** → 另一个
session 改完提交 → 再读。少了第一步就走不到被测分支（「造的样本没走到被测
分支 = 没测」）。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.controller.ws import _audience_at_speaker_location, _push_keeper_phase
from app.core.db import Base
from app.models.room import Player, Room

if TYPE_CHECKING:
    from fastapi import WebSocket


class _StubSocket:
    """只收 `send_json`——`only_player_id` 那条路径直接发给这一条连接。

    不去构造真的 `WebSocket`：这条用例要证的是"读到的是哪一份状态"，
    连接本身只是个出口。
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/fresh.db", poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # 🔴 `expire_on_commit=False` 跟 `app.core.db` 一致 —— 这正是缺陷的一环，
    # 测试装置照抄生产配置，不然就是在验一个不存在的前置。
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(factory, keeper_state: dict) -> tuple[str, list[str]]:
    room_id = uuid.uuid4().hex
    async with factory() as db:
        db.add(
            Room(
                id=room_id,
                room_code="FRSH01",
                room_name="新鲜度",
                max_players=4,
                phase="InGame",
                keeper_state=keeper_state,
            )
        )
        await db.flush()
        players = []
        for i in range(2):
            p = Player(room_id=room_id, nickname=f"玩家{i}", is_host=(i == 0))
            db.add(p)
            players.append(p)
        await db.flush()
        ids = [p.id for p in players]
        await db.commit()
    return room_id, ids


async def _turn_writes(factory, room_id: str, keeper_state: dict) -> None:
    """这一拍：narrator 用**它自己的** session 写回并提交。"""
    async with factory() as turn_db:
        room = await turn_db.get(Room, room_id)
        room.keeper_state = keeper_state
        await turn_db.commit()


async def test_ws_reads_fresh_state(factory) -> None:
    """🔴 `keeper.phase` 必须推这一拍写下的那个值。

    变异体：把 `_fresh_room` 改回 `db.get(Room, room_id)` ⇒ 推出去的是
    `investigation`，这条当场红。
    """
    room_id, player_ids = await _seed(factory, {"对局阶段": "investigation"})

    async with factory() as ws_db:
        # ① 这条消息一进来就读过一次（= `find_room_by_id`），Room 进 identity map
        #
        # 🔴 **必须接住这个变量**：identity map 是弱引用，不接的话对象当场被回收、
        # 下次 `get` 反而会重查，变异体就活下来了（第一版正是这样写的，把
        # `_fresh_room` 改回旧读法两条用例照样绿）。生产代码里它是
        # `room = await find_room_by_id(db, room_id)`，活到整个 handler 结束。
        stale_ref = await ws_db.get(Room, room_id)
        # ② 这条 session 自己 commit 过一次（= `record_event`）——事务就此结束，
        #    而 expire_on_commit=False 让对象留着旧值
        await ws_db.commit()
        # ③ 这一拍在另一个 session 里写完
        await _turn_writes(factory, room_id, {"对局阶段": "finished", "结局": "truth"})

        assert stale_ref.keeper_state["对局阶段"] == "investigation", (
            "前置没成立：这个对象该还拿着旧值，否则下面等于没测"
        )
        socket = _StubSocket()
        await _push_keeper_phase(
            ws_db,
            cast("WebSocket", socket),
            room_id,
            only_player_id=player_ids[0],
        )

    assert socket.sent, "没推出去任何东西"
    payload = socket.sent[-1]["payload"]
    assert payload["phase"] == "finished", (
        "推给玩家的阶段还是这一拍之前的 —— 而 finished 那一拍后面没有下一拍来纠正它"
    )
    assert payload["endingId"] == "truth"


async def test_the_delivery_audience_reads_fresh_state(factory) -> None:
    """🔴 投递受众也按这一拍之后的位置算。

    读旧位置的后果不是"显示滞后"：这一拍刚走开的人，他的检定卡会按**移动前**
    的同处关系投出去 —— 分头局里那是隔离泄漏。
    """
    room_id, (a, b) = await _seed(factory, {})
    # 开局两个人在一处
    await _turn_writes(factory, room_id, {"玩家位置": f"{a}@hall, {b}@hall"})

    async with factory() as ws_db:
        stale_ref = await ws_db.get(Room, room_id)  # ①（同上，必须接住）
        await ws_db.commit()  # ②
        # ③ 这一拍 A 单独走开了
        await _turn_writes(factory, room_id, {"玩家位置": f"{a}@cellar, {b}@hall"})

        assert stale_ref.keeper_state["玩家位置"].endswith("@hall"), (
            "前置没成立：这个对象该还拿着旧位置，否则下面等于没测"
        )
        audience = await _audience_at_speaker_location(ws_db, room_id, a)

    assert audience is not None, "两个人已经分头了，不该退化成全房间广播"
    assert b not in audience, "按移动前的同处关系投递了 —— B 已经不在 A 那儿了"
    assert a in audience
