"""开场那一拍的在场感（`exec/33 #84`）。

🔴 开场走的是 `narrate()` 里的**独立分支**，`_narrate_per_audience` 里那两个
受众提示（人称 / 旁观者）够不着它——真机双人局因此整段以房主为主角，另一个
玩家从头到尾没出现，而两人收到的是同一段。

这里桩掉 `_narrate_prose` 只看**交给模型的 suffix**：叙事内容是模型写的、
不确定，能确定性断言的只有"代码有没有把在场名单交出去"。
"""

import random
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.runtime.agent import KeeperAgent
from app.core.narration.contract import NarrationContext
from app.models.room import Character, Player, Room

_FIXTURE_MODULE = Path(__file__).parent / "fixtures" / "keeper_module.json"

_db_path = Path(tempfile.mkdtemp(prefix="trpg-opening-cast-test-")) / "opening.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(nicknames: list[str]) -> tuple[str, str]:
    """建房 + 若干带卡玩家，返回 (room_id, 第一个玩家 id)。"""
    async with _session_factory() as db:
        room = Room(room_code="OPEN01", room_name="开场测试房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        first = ""
        for nickname in nicknames:
            player = Player(room_id=room.id, nickname=nickname)
            db.add(player)
            await db.flush()
            first = first or player.id
            db.add(
                Character(
                    room_id=room.id,
                    player_id=player.id,
                    status="complete",
                    name=nickname,
                    occupation="私家侦探",
                    attributes={
                        "STR": 50,
                        "CON": 50,
                        "SIZ": 50,
                        "DEX": 50,
                        "APP": 50,
                        "INT": 50,
                        "POW": 50,
                        "EDU": 50,
                        "LUCK": 50,
                    },  # fmt: skip
                    derived_stats={"HP": 10, "MP": 10, "SAN": 50, "MOV": 8},
                    skills={"spot-hidden": 50},
                )
            )
        await db.commit()
        return room.id, first


class _SuffixSpy(KeeperAgent):
    """只截获交给叙事模型的 suffix，不打网络。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.suffixes: list[str] = []

    async def _narrate_prose(self, *args, **kwargs) -> str:  # type: ignore[override]
        self.suffixes.append(kwargs.get("extra_suffix", ""))
        return "暮色沉下来。"


def _spy() -> _SuffixSpy:
    return _SuffixSpy(
        api_key="fake-key",
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
        rng=random.Random(0),
    )


async def _open(agent: KeeperAgent, room_id: str, player_id: str) -> None:
    await agent.narrate(
        NarrationContext(
            utterance="（开场）",
            player_nickname="阿福",
            room_id=room_id,
            player_id=player_id,
            is_opening_ceremony=True,
        )
    )


async def test_opening_names_everyone_at_the_table() -> None:
    """🔴 两个人以上时，开场必须把在场名单交给模型。

    真机症状：整段只写房主，另一个玩家一次都没出现——他读到的是一段讲别人
    的故事。
    """
    room_id, first = await _seed(["阿福", "阿贵"])
    agent = _spy()

    await _open(agent, room_id, first)

    assert agent.suffixes, "开场那一拍没调到叙事模型"
    suffix = agent.suffixes[0]
    assert "阿福" in suffix and "阿贵" in suffix
    assert "都在场" in suffix


async def test_single_player_opening_keeps_second_person() -> None:
    """单人局：只给人称提示（「你」），不塞在场名单——一个人不存在"谁没出现"。"""
    room_id, first = await _seed(["阿福"])
    agent = _spy()

    await _open(agent, room_id, first)

    suffix = agent.suffixes[0]
    assert "第二人称" in suffix
    assert "都在场" not in suffix
