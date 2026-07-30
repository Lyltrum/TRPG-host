"""基于磁带回放的 keeper 回归测试（exec/14 P0）。

跟 `test_llm_tape.py`（验证磁带机制本身）不同，这里跑的是**真正的
`KeeperAgent` 全管线**——裁决 → 执行 → 叙事 → scrub → 落库——只是模型输出
来自录好的磁带而不是网络。

它守的是 P1/P2 那次重构的底线：**同样的模型输出，行为必须不变。**

磁带 `tests/tapes/keeper_minimal.json` 用原创迷你剧本录制（可进 git，
见 test_llm_tape.py 的版权守卫）。重录方式：

    .venv/bin/python scripts/record_keeper_tape.py \\
        --module tests/fixtures/keeper_module.json \\
        --out tests/tapes/keeper_minimal.json
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.agent import KeeperAgent
from app.core.keeper.module_loader import load_module
from app.core.llm_tape import Tape, replaying
from app.core.narrator import NarrationContext
from app.models.room import Character, Player, Room

TAPE_PATH = Path(__file__).parent / "tapes" / "keeper_minimal.json"
MODULE_PATH = Path(__file__).parent / "fixtures" / "keeper_module.json"

#: 与录制时（scripts/record_keeper_tape.py 的 DEFAULT_ROUNDS）严格一致，
#: 否则请求指纹对不上（会被 drift 断言抓到）。
ROUNDS = [
    "我仔细查看四周，有什么不对劲的地方吗？",
    "我现在该做什么？",
]


async def _fresh_room(tmp_path: Path):
    """临时库 + 干净房间 + 一张已就绪的角色卡。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/replay.db", poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as db:
        room = Room(
            room_code="RPLAY1",
            room_name="回放房",
            max_players=4,
            phase="InGame",
            keeper_state={},
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="调查员甲", is_host=True)
        db.add(player)
        await db.flush()
        db.add(
            Character(
                room_id=room.id,
                player_id=player.id,
                status="ready",
                name="调查员甲",
                occupation="私家侦探",
                generation_method="pointbuy",
                attributes={
                    "STR": 50,
                    "CON": 60,
                    "POW": 55,
                    "DEX": 65,
                    "APP": 50,
                    "SIZ": 55,
                    "INT": 70,
                    "EDU": 75,
                    "LUCK": 60,
                },
                derived_stats={"HP": 11, "HP_MAX": 11, "MP": 11, "SAN": 55, "SAN_MAX": 55},
                skills={
                    "spot-hidden": 60,
                    "listen": 50,
                    "library-use": 50,
                    "psychology": 45,
                    "fast-talk": 40,
                    "stealth": 40,
                    "fighting-brawl": 40,
                },
                background="",
                notes="",
            )
        )
        player.has_character = True
        await db.commit()
        ids = (room.id, player.id, player.nickname)

    return engine, session_factory, ids


async def _play(tmp_path: Path) -> tuple[list[str], dict, list]:
    """按磁带跑完整两轮，返回（每轮叙事正文、房间 keeper_state、漂移列表）。"""
    engine, session_factory, (room_id, player_id, nickname) = await _fresh_room(tmp_path)
    keeper = KeeperAgent(
        api_key="tape-replay-no-network",
        module=load_module(MODULE_PATH),
        ruleset=build_coc7_ruleset(),
        session_factory=session_factory,
    )

    texts: list[str] = []
    with replaying(TAPE_PATH) as session:
        for utterance in ROUNDS:
            outcome = await keeper.narrate(
                NarrationContext(
                    utterance=utterance,
                    player_nickname=nickname,
                    room_id=room_id,
                    player_id=player_id,
                )
            )
            texts.append(outcome.text)

    async with session_factory() as db:
        room = await db.get(Room, room_id)
        keeper_state = dict(room.keeper_state or {})

    await engine.dispose()
    return texts, keeper_state, session.drifts


@pytest.mark.asyncio
async def test_replay_is_deterministic(tmp_path: Path) -> None:
    """同一盘磁带跑两遍，逐字一致——这是「重构没改行为」这条断言的基础。"""
    first, _, _ = await _play(tmp_path / "a")
    second, _, _ = await _play(tmp_path / "b")
    assert first == second
    assert all(t.strip() for t in first)


@pytest.mark.asyncio
async def test_replay_request_digests_match_recording(tmp_path: Path) -> None:
    """没有漂移 = 上下文组装（system prompt / 历史重放 / 局面块）没变。

    这条变红不代表出 bug，代表 prompt 变了——需要人判断是不是预期内的改动，
    是的话重录磁带。P1 事实寻址会改 `render_full`，届时这条预期会红。
    """
    _, _, drifts = await _play(tmp_path)
    assert drifts == [], f"请求指纹与录制时不符：{drifts}"


@pytest.mark.asyncio
async def test_scrub_only_deletes_never_invents(tmp_path: Path) -> None:
    """叙事纪律层只许删，不许加字——输出必须是录音正文的子序列。

    prose_discipline 的整个设计前提就是「事后删掉越界的句子」；哪天它开始
    往正文里添内容（或改写），这条会红。
    """
    texts, _, _ = await _play(tmp_path)
    tape = Tape.load(TAPE_PATH)
    recorded_narrations = [e.response_text for e in tape.entries if e.kind == "narrate"]
    assert len(recorded_narrations) == len(texts)

    for produced, recorded in zip(texts, recorded_narrations, strict=True):
        it = iter(recorded)
        assert all(ch in it for ch in produced), "叙事正文出现了录音里没有的内容"


@pytest.mark.asyncio
async def test_state_updates_are_persisted(tmp_path: Path) -> None:
    """裁决写的 state_updates 真的落进 rooms.keeper_state（v1 时期这里长期是空的）。"""
    _, keeper_state, _ = await _play(tmp_path)
    assert keeper_state, "整局跑完 keeper_state 仍为空——记账链路断了"
    assert "当前场景" in keeper_state
