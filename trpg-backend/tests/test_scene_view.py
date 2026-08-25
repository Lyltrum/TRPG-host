"""「现场」抽屉的服务层（`exec/46` B4）。

守的是**受众裁剪**，不是取数——取数错了看得见，裁剪错了是当场泄密而且没有任何
东西会变红。

三条边界：
1. 线索按 `audience` 逐人裁（事实账本从第一天就是这么记的）；
2. **分头期间**在场 NPC 与足迹一律不给——它们在 `keeper_state` 里是**房间级**的，
   没有任何字段记着"这个 NPC 站在哪一组面前"；
3. 拿不到模组时**原样给 id，不编造名字**（同 `location_label` 的先例）。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import Base
from app.core.keeper.capabilities.cast.state import ON_STAGE_KEY
from app.core.keeper.memory.fact_ledger import EVENT_TYPE as FACT_EVENT_TYPE
from app.core.keeper.runtime.location_state import PLAYER_LOCATION_KEY
from app.core.keeper.runtime.phase import PHASE_INVESTIGATION, PHASE_KEY
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY
from app.models.event import Event
from app.models.room import Player, Room
from app.service.scene import VISITED_KEY, get_scene

_db_path = Path(tempfile.mkdtemp(prefix="trpg-scene-test-")) / "scene.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


class _FakeNarrator:
    """只做 id → 文本，跟 `RoomAwareKeeperNarrator.scene_labels` 同形状。"""

    def __init__(self) -> None:
        self.seen: dict = {}

    async def scene_labels(self, room_id, keeper_state, *, npc_ids, node_ids, fact_ids):  # noqa: ANN001
        self.seen = {
            "npc_ids": list(npc_ids),
            "node_ids": list(node_ids),
            "fact_ids": set(fact_ids),
        }
        names = {"butler": "管家", "maid": "女佣"}
        places = {"hall": "门厅", "cellar": "地下室", "kitchen": "厨房"}
        clues = {"f-hall": "门厅的湿泥脚印", "f-cellar": "地下室的银器箱"}
        return (
            {i: names.get(i, i) for i in npc_ids},
            {i: places.get(i, i) for i in node_ids},
            {i: t for i, t in clues.items() if i in fact_ids},
        )


async def _seed(room_code: str, *, split: bool):
    """两人房。`split=True` 时阿贵在地下室（= 分头）。"""
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="现场房",
            max_players=4,
            phase="InGame",
            keeper_state={
                PHASE_KEY: PHASE_INVESTIGATION,
                CURRENT_NODE_KEY: "hall",
                ON_STAGE_KEY: "butler, maid",
                VISITED_KEY: "kitchen, hall",
            },
        )
        db.add(room)
        await db.flush()
        a = Player(room_id=room.id, nickname="阿福")
        b = Player(room_id=room.id, nickname="阿贵")
        db.add_all([a, b])
        await db.flush()
        if split:
            room.keeper_state = {
                **(room.keeper_state or {}),
                PLAYER_LOCATION_KEY: f"{b.id}@cellar",
            }
        db.add_all(
            [
                Event(
                    room_id=room.id,
                    player_id=a.id,
                    event_type=FACT_EVENT_TYPE,
                    payload={"fact_id": "f-hall", "via": "check", "audience": [a.id]},
                ),
                Event(
                    room_id=room.id,
                    player_id=b.id,
                    event_type=FACT_EVENT_TYPE,
                    payload={"fact_id": "f-cellar", "via": "check", "audience": [b.id]},
                ),
            ]
        )
        await db.commit()
        return room.id, a.id, b.id


async def test_each_player_only_sees_the_clues_he_earned() -> None:
    """🔴 线索按受众裁：阿福看不到阿贵在地下室挣到的那条。

    变异检验：把 `visible_fact_ids` 的 audience 换成 `None`（守秘人视图），
    这条立刻红。
    """
    room_id, a_id, b_id = await _seed("SCN001", split=False)
    async with _session_factory() as db:
        a = await db.get(Player, a_id)
        b = await db.get(Player, b_id)
        assert a is not None and b is not None
        mine = await get_scene(db, room_id, a, narrator=_FakeNarrator())
        his = await get_scene(db, room_id, b, narrator=_FakeNarrator())
    assert [c.text for c in mine.clues] == ["门厅的湿泥脚印"]
    assert [c.text for c in his.clues] == ["地下室的银器箱"]


async def test_stage_and_trail_are_withheld_while_the_party_is_split() -> None:
    """🔴 分头期间在场 NPC 与足迹一律不给，而且**不是取了再挡**。

    这两样在 keeper_state 里是房间级的，照给就是把别处那一组的处境泄给你。
    断言连"服务层根本没去查它们"一起钉住：`_FakeNarrator` 收到的 id 必须是空的
    ——否则下一个人很容易在中间插一段"顺手也用一下"，那时泄漏无声无息。
    """
    room_id, a_id, _b = await _seed("SCN002", split=True)
    narrator = _FakeNarrator()
    async with _session_factory() as db:
        a = await db.get(Player, a_id)
        assert a is not None
        scene = await get_scene(db, room_id, a, narrator=narrator)
    assert scene.split_now is True
    assert scene.npcs_on_stage == []
    assert scene.visited_places == []
    assert narrator.seen["npc_ids"] == [] and narrator.seen["node_ids"] == []
    # 线索不受分头影响——它自带 audience，本来就是逐人裁的
    assert [c.text for c in scene.clues] == ["门厅的湿泥脚印"]


async def test_together_the_stage_and_trail_are_given() -> None:
    """退化保证：没分头时照给，而且名字是解析过的。"""
    room_id, a_id, _b = await _seed("SCN003", split=False)
    async with _session_factory() as db:
        a = await db.get(Player, a_id)
        assert a is not None
        scene = await get_scene(db, room_id, a, narrator=_FakeNarrator())
    assert scene.split_now is False
    assert [n.name for n in scene.npcs_on_stage] == ["管家", "女佣"]
    assert [p.name for p in scene.visited_places] == ["厨房", "门厅"]
    # id 必须一起给：它是玩家看得见指针、报得出指针错的唯一依据（`exec/46` A1）
    assert [p.id for p in scene.visited_places] == ["kitchen", "hall"]


async def test_without_a_narrator_the_ids_are_given_raw() -> None:
    """🔴 解析不出模组时**原样给 id，不编造名字**（同 `location_label`）。"""
    room_id, a_id, _b = await _seed("SCN004", split=False)
    async with _session_factory() as db:
        a = await db.get(Player, a_id)
        assert a is not None
        scene = await get_scene(db, room_id, a, narrator=None)
    assert [n.name for n in scene.npcs_on_stage] == ["butler", "maid"]
    assert [p.name for p in scene.visited_places] == ["kitchen", "hall"]
    # 🔴 线索**给空**而不是给一串裸 fact-id：那对玩家没有任何意义
    assert scene.clues == []


async def test_empty_state_is_not_a_crash() -> None:
    """开局那一刻三样都是空的，不该炸也不该编。"""
    async with _session_factory() as db:
        room = Room(room_code="SCN005", room_name="空房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        p = Player(room_id=room.id, nickname="阿福")
        db.add(p)
        await db.commit()
        room_id, pid = room.id, p.id
    async with _session_factory() as db:
        player = await db.get(Player, pid)
        assert player is not None
        scene = await get_scene(db, room_id, player, narrator=_FakeNarrator())
    assert scene.npcs_on_stage == [] and scene.visited_places == [] and scene.clues == []
    assert scene.split_now is False
