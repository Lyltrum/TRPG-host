"""health 能力的验收（跟能力代码放在同一个目录里，exec/27 阶段 2）。

真人实测 2026-07-31 日志：
    keeper_decision_issues issues=['HP 变更未执行：找不到玩家「科比特」…']
战斗里 NPC 当然会掉血，而 HP 变更此前只认房间里的**玩家**，于是 NPC 的伤势
完全没有落点、只活在叙事文字里，下一轮裁决器只能从上一段散文里猜它伤到
什么程度。

这里验证四件事：
1. 名字解析走**白名单**（模组 npc id / name / 形态），解析不出就报错、不新建条目；
2. 有数据卡 HP → 维护 hp；没有 → 显式降级成累计伤害（不编初始值）；
3. 调查员那条路（角色卡 derived_stats，首次修改备份上限）；
4. 执行层按 `npc` 字段分流，且状态真的写进 keeper_state、渲染进局面块。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities.health.executor import adjust_hp_impl, adjust_npc_hp_impl
from app.core.keeper.capabilities.health.npc_state import (
    NPC_STATE_KEY,
    apply_hp_delta,
    format_npc_states,
    initial_hp,
    load_npc_states,
)
from app.core.keeper.capabilities.health.schema import HpChange
from app.core.keeper.decision import KeeperDecision
from app.core.keeper.deps import KeeperDeps, KeeperToolError
from app.core.keeper.module_loader import load_module
from app.core.keeper.primitives.npcs import resolve_npc_id
from app.core.keeper.tools import update_state_impl
from app.core.keeper.turn_executor import execute_side_effects
from app.models.room import Character, Player, Room

_REPO_TESTS = Path(__file__).resolve().parents[5] / "tests"
_FIXTURE_MODULE = str(_REPO_TESTS / "fixtures" / "keeper_module.json")
_MODULE = load_module(_FIXTURE_MODULE)

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-npc-test-")) / "npc.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ── 1. 白名单解析 ───────────────────────────────────


def test_resolve_by_id_name_and_form() -> None:
    assert resolve_npc_id(_MODULE, "butler-secret") == "butler-secret"
    # name 有两个同名 NPC（公开侧/秘密侧）→ 取先出现的那个，不猜
    assert resolve_npc_id(_MODULE, "管家") == "butler-public"
    # 形态 id 归属它所属的 NPC
    assert resolve_npc_id(_MODULE, "butler-cornered") == "butler-secret"


def test_unknown_name_resolves_to_none_not_a_new_entry() -> None:
    """🔴 解析不出就是解析不出——凭裁决器随口写的名字建条目，等于让自由文本
    当标识符，下一轮换个称呼就成了两个 NPC。"""
    assert resolve_npc_id(_MODULE, "科比特") is None
    assert resolve_npc_id(_MODULE, "") is None


# ── 2. 两条记账各自自洽 ──────────────────────────────


def test_hp_path_tracks_remaining_and_floors_at_zero() -> None:
    assert initial_hp(_MODULE, "butler-secret") == 10
    states: dict[str, dict] = {}
    assert apply_hp_delta(states, "butler-secret", -4, base_hp=10) == {"hp": 6}
    assert apply_hp_delta(states, "butler-secret", -9, base_hp=10) == {"hp": 0}


def test_missing_hp_degrades_to_cumulative_damage_not_a_fake_hp() -> None:
    """数据卡没有 HP 时**不编一个初始值**：记累计伤害，渲染时也说清楚。

    静默兜底（`?? 10`）会造出一个假血条，裁决器据此判断"还能打几轮"，
    而那个数字没有任何依据。
    """
    states: dict[str, dict] = {}
    assert apply_hp_delta(states, "ghoul", -4, base_hp=None) == {"damage": 4}
    assert apply_hp_delta(states, "ghoul", -3, base_hp=None) == {"damage": 7}
    # 治疗也走同一条路：累计伤害减少，下限 0
    assert apply_hp_delta(states, "ghoul", 10, base_hp=None) == {"damage": 0}


def test_format_renders_nothing_when_no_npc_touched() -> None:
    assert format_npc_states(_MODULE, {}) == ""
    assert format_npc_states(_MODULE, {"当前场景": "书房"}) == ""


def test_format_marks_downed_npc() -> None:
    text = format_npc_states(_MODULE, {NPC_STATE_KEY: {"butler-secret": {"hp": 0}}})
    assert "管家" in text and "HP 0" in text and "已倒地" in text


# ── 3. 调查员那条路 / 4. 执行层接线 ────────────────


async def _seed(room_code: str) -> tuple[str, str]:
    async with _session_factory() as db:
        room = Room(room_code=room_code, room_name="NPC 房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="阿福")
        db.add(player)
        await db.flush()
        await db.commit()
        return room.id, player.id


def _deps(room_id: str, player_id: str) -> KeeperDeps:
    return KeeperDeps(
        room_id=room_id,
        player_id=player_id,
        session_factory=_session_factory,
        module=_MODULE,
        ruleset=build_coc7_ruleset(),
    )


async def test_adjust_npc_hp_writes_keeper_state() -> None:
    room_id, player_id = await _seed("NPC001")
    deps = _deps(room_id, player_id)
    await adjust_npc_hp_impl(deps, -4, "被铁铲砍中", "管家")

    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        assert load_npc_states(room.keeper_state) == {"butler-public": {"hp": 6}}


async def test_adjust_unknown_npc_raises_and_lists_cast() -> None:
    room_id, player_id = await _seed("NPC002")
    with pytest.raises(KeeperToolError) as exc:
        await adjust_npc_hp_impl(_deps(room_id, player_id), -4, "打了一下", "科比特")
    assert "管家" in str(exc.value)


async def test_executor_routes_npc_field_without_touching_players() -> None:
    """🔴 这条是 #39 的回归点：以前这笔账走 `_resolve_character`，
    报"找不到玩家「管家」"、整笔丢失。"""
    room_id, player_id = await _seed("NPC003")
    deps = _deps(room_id, player_id)
    decision = KeeperDecision(
        thinking="砍中管家",
        narration_guidance="写打斗",
        hp_changes=[HpChange(delta=-4, npc="管家", reason="被铁铲砍中")],
    )
    report, issues = await execute_side_effects(deps, decision)
    assert issues == []
    assert any("管家" in line for line in report)

    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        assert load_npc_states(room.keeper_state)["butler-public"]["hp"] == 6


async def _seed_with_character(room_code: str) -> tuple[str, str]:
    room_id, player_id = await _seed(room_code)
    async with _session_factory() as db:
        db.add(
            Character(
                room_id=room_id,
                player_id=player_id,
                name="阿福",
                attributes={"STR": 50, "CON": 50},
                skills={},
                derived_stats={"HP": 10, "SAN": 55, "MP": 11},
            )
        )
        await db.commit()
    return room_id, player_id


async def test_adjust_player_hp_damage_and_floor() -> None:
    """调查员那条路：改角色卡的 derived_stats，首次修改把上限备份成 HP_MAX。"""
    room_id, player_id = await _seed_with_character("NPC004")
    deps = _deps(room_id, player_id)

    text = await adjust_hp_impl(deps, -3, "被食尸鬼抓伤")
    assert "10 → 7" in text

    text = await adjust_hp_impl(deps, -99, "致命打击")
    assert "→ 0" in text and "倒地" in text
    # 两次 HP 变动都进了可见性记录——掷骰/伤害的可见性不靠模型自觉
    assert len(deps.check_results) == 2

    async with _session_factory() as db:
        character = await db.get(Character, (await _character_id(room_id)))
        assert character is not None
        derived = character.derived_stats or {}
        assert derived["HP"] == 0
        assert derived["HP_MAX"] == 10


async def _character_id(room_id: str) -> str:
    from sqlalchemy import select

    async with _session_factory() as db:
        result = await db.execute(select(Character).where(Character.room_id == room_id))
        character = result.scalars().first()
        assert character is not None
        return character.id


async def test_state_updates_cannot_clobber_the_npc_ledger() -> None:
    """🔴 回归（exec/27 阶段 3 复查复现出来的真数据丢失路径）。

    `NPC状态` 此前既不在"保留键"里也不在"不喂给模型的键"里，于是裁决器写一条
    `{"key": "NPC状态", "value": "管家看起来还行"}` 就能把那个 dict 覆盖成字符串，
    `load_npc_states` 随即返回 `{}`——**全部 NPC 血量记录静默消失**，日志里什么
    都没有。修法不是往两张清单各补一行，是让第二张清单不再独立存在。
    """
    room_id, player_id = await _seed("NPC005")
    deps = _deps(room_id, player_id)
    await adjust_npc_hp_impl(deps, -4, "被铁铲砍中", "管家")

    with pytest.raises(KeeperToolError) as exc:
        await update_state_impl(deps, NPC_STATE_KEY, "管家看起来还行")
    assert "系统记账" in str(exc.value)

    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        assert load_npc_states(room.keeper_state) == {"butler-public": {"hp": 6}}
