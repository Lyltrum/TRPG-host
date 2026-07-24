"""提案①议程标注：模组 schema 时间维度 + 裁决消费 once 记账。

覆盖 module_loader 新模型/渲染、tools 的 load/mark、decision 的 agenda_fired、
prompts 的议程状态注入。fixture 是原创迷你庄园失窃案延伸——与任何第三方
模组原文无关。
"""

import asyncio
import json
import random
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.decision import KeeperDecision, execute_side_effects
from app.core.keeper.module_loader import (
    AgendaTrigger,
    load_module,
    render_agenda_trigger,
    render_full,
)
from app.core.keeper.prompts import format_agenda_status, format_turn_input
from app.core.keeper.tools import (
    AGENDA_FIRED_KEY,
    KeeperDeps,
    load_fired_agenda,
    mark_agenda_fired_impl,
)
from app.models.event import Event
from app.models.room import Character, Player, Room

_FIXTURE_MODULE = Path(__file__).parent / "fixtures" / "keeper_module.json"

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-agenda-test-")) / "agenda.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def deps() -> KeeperDeps:
    async with _session_factory() as db:
        room = Room(room_code="AGND01", room_name="议程房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        actor = Player(room_id=room.id, nickname="阿福")
        db.add(actor)
        await db.flush()
        db.add(
            Character(
                room_id=room.id,
                player_id=actor.id,
                status="complete",
                name="侦探福",
                occupation="私家侦探",
                age=32,
                gender="男",
                attributes={
                    "STR": 60,
                    "CON": 50,
                    "SIZ": 50,
                    "DEX": 70,
                    "APP": 50,
                    "INT": 80,
                    "POW": 50,
                    "EDU": 70,
                    "LUCK": 55,
                },
                derived_stats={"HP": 10, "MP": 10, "SAN": 50, "MOV": 8},
                skills={"spot-hidden": 70},
            )
        )
        await db.commit()
        room_id, actor_id = room.id, actor.id

    return KeeperDeps(
        room_id=room_id,
        player_id=actor_id,
        session_factory=_session_factory,
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        rng=random.Random(42),
    )


# ── 1. 向后兼容 ─────────────────────────────────────


def test_load_module_without_agenda_fields_is_backward_compatible(tmp_path: Path) -> None:
    """不含 opening/agenda/trigger 的最小 JSON 必须照常 load 成功。"""
    minimal = {
        "meta": {"id": "old", "title": "旧模组"},
        "kp_truth": {"summary": "旧真相"},
        "player_intro": "开场白",
        "endings": [{"id": "e1", "title": "结局", "text": "完"}],
    }
    path = tmp_path / "old.json"
    path.write_text(json.dumps(minimal, ensure_ascii=False), encoding="utf-8")
    module = load_module(path)
    assert module.agenda == []
    assert module.opening is None
    assert module.endings[0].trigger is None


# ── 2. fixture 载入 ─────────────────────────────────


def test_fixture_loads_agenda_and_opening() -> None:
    module = load_module(_FIXTURE_MODULE)
    assert len(module.agenda) == 2
    night = module.agenda_by_id("night-1-footprints")
    assert night is not None
    assert night.trigger.type == "game_night" and night.trigger.at == 1
    assert night.once is True
    manual = module.agenda_by_id("butler-confession-window")
    assert manual is not None
    assert manual.trigger.type == "manual" and manual.once is False
    assert module.agenda_by_id("no-such-id") is None
    assert module.opening is not None
    assert module.opening.scene == "庄园门厅"
    assert module.endings[0].trigger == "玩家当众指认管家且出示钥匙或手套证据"


# ── 3. render_full 块与空块省略 ─────────────────────


def test_render_full_includes_opening_and_secret_agenda() -> None:
    text = render_full(load_module(_FIXTURE_MODULE))
    assert "【开场脚本】" in text
    assert "庄园门厅" in text
    assert "【议程时间轴（绝密）】" in text
    assert "night-1-footprints" in text
    assert "绝密" in text


def test_render_full_omits_empty_agenda_and_opening_blocks(tmp_path: Path) -> None:
    minimal = {
        "meta": {"id": "old", "title": "旧模组"},
        "kp_truth": {"summary": "旧真相"},
        "player_intro": "开场白",
        "nodes": [],
        "endings": [{"id": "e1", "title": "结局", "text": "完"}],
        "npcs": [],
    }
    path = tmp_path / "old.json"
    path.write_text(json.dumps(minimal, ensure_ascii=False), encoding="utf-8")
    text = render_full(load_module(path))
    assert "议程" not in text
    assert "开场脚本" not in text


# ── 4. render_agenda_trigger ────────────────────────


def test_render_agenda_trigger_known_and_unknown() -> None:
    assert render_agenda_trigger(AgendaTrigger(type="game_night", at=2)) == "第 2 个游戏内夜晚"
    assert render_agenda_trigger(AgendaTrigger(type="silence", seconds=90)) == "现实沉默 90 秒后"
    assert (
        render_agenda_trigger(AgendaTrigger(type="manual", note="玩家掌握钥匙证据后"))
        == "KP 裁量：玩家掌握钥匙证据后"
    )
    # 未知 type 不抛，带出原始信息
    unknown = render_agenda_trigger(AgendaTrigger(type="weather", at=3, note="暴雨"))
    assert "weather" in unknown and "at=3" in unknown and "note=暴雨" in unknown


# ── 5. format_agenda_status ─────────────────────────


def test_format_agenda_status_partitions_and_once_false() -> None:
    module = load_module(_FIXTURE_MODULE)
    # 全未触发
    all_pending = format_agenda_status(module, [])
    assert "尚未发生" in all_pending
    assert "night-1-footprints" in all_pending
    assert "butler-confession-window" in all_pending
    assert "已经发生" not in all_pending

    # 部分触发：once=True 进已发生；once=False 仍留未发生
    partial = format_agenda_status(module, ["night-1-footprints", "butler-confession-window"])
    assert "### 已经发生" in partial
    assert "night-1-footprints" in partial
    # once=False 即使已触发仍在未发生区
    pending_section, done_section = partial.split("### 已经发生")
    assert "butler-confession-window" in pending_section
    assert "butler-confession-window" not in done_section
    assert "night-1-footprints" in done_section


def test_format_agenda_status_empty_agenda_returns_empty(tmp_path: Path) -> None:
    minimal = {
        "meta": {"id": "old", "title": "旧"},
        "kp_truth": {"summary": "x"},
        "player_intro": "y",
    }
    path = tmp_path / "empty.json"
    path.write_text(json.dumps(minimal, ensure_ascii=False), encoding="utf-8")
    assert format_agenda_status(load_module(path), []) == ""


# ── 6. load_fired_agenda ────────────────────────────


def test_load_fired_agenda_parses_robustly() -> None:
    assert load_fired_agenda(None) == []
    assert load_fired_agenda({}) == []
    assert load_fired_agenda({AGENDA_FIRED_KEY: ""}) == []
    assert load_fired_agenda({AGENDA_FIRED_KEY: "a, b ,"}) == ["a", "b"]
    assert load_fired_agenda({AGENDA_FIRED_KEY: "x,y,z"}) == ["x", "y", "z"]


# ── 7. mark_agenda_fired_impl ───────────────────────


async def test_mark_agenda_fired_first_write_and_once_idempotent(deps: KeeperDeps) -> None:
    report = await mark_agenda_fired_impl(deps, ["night-1-footprints"])
    assert "night-1-footprints" in report
    assert "已触发过" not in report
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert load_fired_agenda(room.keeper_state) == ["night-1-footprints"]

    # 重复 once=True → 幂等，状态不重复，措辞含"已触发过"
    report2 = await mark_agenda_fired_impl(deps, ["night-1-footprints"])
    assert "已触发过" in report2
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert load_fired_agenda(room.keeper_state) == ["night-1-footprints"]

    # 只留一次 keeper.agenda 事件（第二次纯跳过不写库）
    async with _session_factory() as db:
        result = await db.execute(select(Event).where(Event.event_type == "keeper.agenda"))
        agenda_events = list(result.scalars())
    assert len(agenda_events) == 1
    assert agenda_events[0].payload["event_ids"] == ["night-1-footprints"]


async def test_mark_agenda_fired_once_false_can_repeat(deps: KeeperDeps) -> None:
    eid = "butler-confession-window"
    r1 = await mark_agenda_fired_impl(deps, [eid])
    assert eid in r1 and "已触发过" not in r1
    r2 = await mark_agenda_fired_impl(deps, [eid])
    # once=False 再次触发：不说"已触发过"，列表不重复
    assert eid in r2 and "已触发过" not in r2
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert load_fired_agenda(room.keeper_state) == [eid]


async def test_mark_agenda_fired_concurrent_two_ids(deps: KeeperDeps) -> None:
    """并发标记两个不同 id，两个都要存活（write_lock 防 lost update）。"""
    await asyncio.gather(
        mark_agenda_fired_impl(deps, ["night-1-footprints"]),
        mark_agenda_fired_impl(deps, ["butler-confession-window"]),
    )
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        fired = set(load_fired_agenda(room.keeper_state))
        assert fired == {"night-1-footprints", "butler-confession-window"}


# ── 8. execute_side_effects 处理 agenda_fired ───────


async def test_execute_side_effects_unknown_agenda_id_is_issue(deps: KeeperDeps) -> None:
    decision = KeeperDecision(agenda_fired=["no-such-event"])
    report, issues = await execute_side_effects(deps, decision)
    assert any("no-such-event" in i for i in issues)
    assert report == []  # 无效 id 不进 mark
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert load_fired_agenda(room.keeper_state) == []


async def test_execute_side_effects_valid_agenda_updates_state(deps: KeeperDeps) -> None:
    decision = KeeperDecision(agenda_fired=["night-1-footprints"])
    report, issues = await execute_side_effects(deps, decision)
    assert issues == []
    assert len(report) == 1 and "night-1-footprints" in report[0]
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        assert "night-1-footprints" in load_fired_agenda(room.keeper_state)


# ── 9. KeeperDecision 解析 ──────────────────────────


def test_keeper_decision_parses_agenda_fired() -> None:
    d = KeeperDecision.model_validate_json(
        '{"thinking": "夜已深", "agenda_fired": ["night-1-footprints"], '
        '"narration_guidance": "呈现响动"}'
    )
    assert d.agenda_fired == ["night-1-footprints"]


def test_keeper_decision_agenda_fired_defaults_empty() -> None:
    d = KeeperDecision.model_validate_json('{"thinking": "无事"}')
    assert d.agenda_fired == []


# ── 10. format_turn_input 默认不渲染议程块 ──────────


def test_format_turn_input_empty_agenda_status_omits_block() -> None:
    text = format_turn_input(
        {"当前场景": "门厅"},
        ["阿福：我检查脚印"],
        ["阿福（角色：侦探福）"],
        "阿福",
        "我下地下室",
        agenda_status="",
    )
    assert "议程状态" not in text
    # 旧调用点不传 agenda_status 也应如此
    text2 = format_turn_input({"k": "v"}, [], ["阿福"], "阿福", "你好")
    assert "议程状态" not in text2


def test_format_turn_input_with_agenda_status_renders_block() -> None:
    text = format_turn_input(
        None,
        [],
        ["阿福"],
        "阿福",
        "等到入夜",
        agenda_status="- night-1-footprints · 第一夜",
    )
    assert "## 议程状态" in text
    assert "night-1-footprints" in text
    # 块位置：世界状态之后、游戏历史之前
    state_pos = text.index("世界状态笔记")
    agenda_pos = text.index("议程状态")
    history_pos = text.index("游戏历史")
    assert state_pos < agenda_pos < history_pos
