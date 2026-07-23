"""KeeperAgent 的接线与记忆层（不跑真实 LLM）。

- build_narrator 的三档选择：keeper（模组+key 都配）> DeepSeek（只有 key）>
  Fallback；配了模组路径但没配 key 时**不能**误入 keeper；
- _load_room_memory 的全量重放：events 表 → 格式化历史行 + 状态笔记。
  构造 KeeperAgent 只建客户端对象、不发网络请求，fake key 即可。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.config import Settings
from app.core.db import Base
from app.core.keeper.agent import KeeperAgent
from app.core.keeper.module_loader import load_module
from app.core.keeper.prompts import format_turn_input
from app.core.narrator import DeepSeekNarrator, FallbackNarrator, NarrationContext, build_narrator
from app.models.event import Event
from app.models.room import Player, Room

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-agent-test-")) / "agent.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _keeper(api_key: str = "fake-key") -> KeeperAgent:
    return KeeperAgent(
        api_key=api_key,
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        session_factory=_session_factory,
    )


# ── build_narrator 选择逻辑 ─────────────────────────


def test_build_narrator_prefers_keeper_when_fully_configured() -> None:
    settings = Settings(deepseek_api_key="k", keeper_module_path=_FIXTURE_MODULE)
    assert isinstance(build_narrator(settings), KeeperAgent)


def test_build_narrator_module_path_without_key_stays_fallback() -> None:
    settings = Settings(deepseek_api_key=None, keeper_module_path=_FIXTURE_MODULE)
    assert isinstance(build_narrator(settings), FallbackNarrator)


def test_build_narrator_key_without_module_path_stays_deepseek() -> None:
    settings = Settings(deepseek_api_key="k", keeper_module_path=None)
    assert isinstance(build_narrator(settings), DeepSeekNarrator)


def test_build_narrator_bad_module_path_fails_loudly() -> None:
    """剧本加载失败必须让启动炸掉，不能静默回退（build_narrator docstring）。"""
    settings = Settings(deepseek_api_key="k", keeper_module_path="/no/such/module.json")
    with pytest.raises(FileNotFoundError):
        build_narrator(settings)


# ── narrate 的前置校验 ──────────────────────────────


async def test_narrate_requires_room_and_player_id() -> None:
    context = NarrationContext(utterance="你好", player_nickname="阿福")  # 没带 ids
    with pytest.raises(ValueError, match="room_id/player_id"):
        await _keeper().narrate(context)


# ── 全量重放（_load_room_memory） ────────────────────


async def test_load_room_memory_replays_events_in_order() -> None:
    async with _session_factory() as db:
        room = Room(
            room_code="KEEP02",
            room_name="记忆房",
            max_players=4,
            phase="InGame",
            keeper_state={"当前场景": "门厅"},
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="阿福")
        db.add(player)
        await db.flush()
        db.add_all(
            [
                Event(
                    room_id=room.id,
                    player_id=player.id,
                    event_type="action.submit",
                    payload={"utterance": "我检查脚印"},
                ),
                Event(
                    room_id=room.id,
                    player_id=player.id,
                    event_type="keeper.check",
                    payload={
                        "player": "阿福",
                        "skill": "侦察",
                        "rolled": 30,
                        "target": 70,
                        "level": "成功",
                    },
                ),
                Event(
                    room_id=room.id,
                    player_id=player.id,
                    event_type="narration.push",
                    payload={"text": "脚印通向地下室。"},
                ),
                # 聊天/状态类事件不进历史（状态另有整体注入；chat 根本不在查询列表里）
                Event(
                    room_id=room.id,
                    player_id=player.id,
                    event_type="keeper.state",
                    payload={"key": "当前场景", "value": "门厅"},
                ),
            ]
        )
        await db.commit()
        room_id = room.id

    keeper_state, lines, roster = await _keeper()._load_room_memory(room_id)

    assert keeper_state == {"当前场景": "门厅"}
    assert lines == [
        "阿福：我检查脚印",
        "[检定] 阿福 侦察：30/70 → 成功",
        "守秘人：脚印通向地下室。",
    ]
    # 在场名单：未建卡的玩家也要出现（agent 不许幻觉出额外的调查员）
    assert roster == ["阿福（未建卡）"]


def test_format_turn_input_contains_all_sections() -> None:
    text = format_turn_input(
        {"当前场景": "门厅"},
        ["阿福：我检查脚印"],
        ["阿福（角色：侦探福，私家侦探）"],
        "阿福",
        "我下地下室",
    )
    assert "当前场景：门厅" in text
    assert "阿福：我检查脚印" in text
    assert "侦探福" in text  # 在场名单注入
    assert "「我下地下室」" in text


def test_format_turn_input_empty_state_hints_game_start() -> None:
    assert "对局刚开始" in format_turn_input(None, [], ["阿福（未建卡）"], "阿福", "开始吧")
