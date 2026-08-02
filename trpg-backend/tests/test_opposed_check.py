"""对抗检定（exec/19 #38）。

真人实测 2026-07-31：叙事正文写着「凌铭辉，进行一次体质对抗检定，目标 POT
16」，界面上却从没出现掷骰卡片——那一轮日志是 `checks=[]`，**裁决器根本没
发起检定**，是叙事阶段自己把要求写进了散文。根因是 `CheckRequest` 表达不了
对抗（只有 skill/player/reason，没有对手目标值），模型想要的东西 schema 里
没位置放，就从正文漏出去了。

这里验证：
1. `dice.resolve_opposed` 的三条判定顺序（等级 → 技能值 → 平局判主动方负）；
2. 目标值越界**跳过整条检定并记 issue**，不悄悄夹紧；
3. 结算时对手侧真的掷了骰，结果进 detail / check_results；
4. 不传 opposed 时整条路径与此前逐字一致（退化保证）。
"""

import random
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys
from app.core.keeper.capabilities.skill_check.executor import roll_check_detail
from app.core.keeper.capabilities.skill_check.schema import CheckRequest, OpposedTarget
from app.core.keeper.decision import KeeperDecision
from app.core.keeper.deps import KeeperDeps
from app.core.keeper.module_loader import load_module
from app.core.keeper.primitives import dice
from app.core.keeper.turn_executor import create_pending_checks
from app.models.room import Character, Player, Room

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")
_MODULE = load_module(_FIXTURE_MODULE)

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-opposed-test-")) / "opposed.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ── 1. 判定规则 ─────────────────────────────────────


def _outcome(rolled: int, target: int) -> dice.CheckOutcome:
    return dice.evaluate_check(rolled, target)


def test_higher_success_level_wins() -> None:
    # 主动方困难成功（30/70）对普通成功（60/70）
    assert dice.resolve_opposed(_outcome(30, 70), _outcome(60, 70)) is True
    assert dice.resolve_opposed(_outcome(60, 70), _outcome(30, 70)) is False


def test_same_level_falls_back_to_higher_skill_value() -> None:
    # 双方都是普通成功，技能值高的赢
    assert dice.resolve_opposed(_outcome(60, 70), _outcome(50, 60)) is True
    assert dice.resolve_opposed(_outcome(50, 60), _outcome(60, 70)) is False


def test_dead_tie_goes_to_the_defender() -> None:
    """平局判主动方**负**——本项目的裁定（规则书说重掷/KP 裁定）。

    真人在等回复，重掷要多一次往返；"平局维持现状"对防守方有利，正是对抗
    检定的常见默认。写死在代码里，不交给模型即兴。
    """
    assert dice.resolve_opposed(_outcome(60, 70), _outcome(60, 70)) is False


def test_actor_who_failed_never_wins() -> None:
    """🔴 试玩实测补的（exec/19 #43）：对抗掷骰里失败的一方不可能获胜。

    初版只比成功等级的序号，于是 `失败(1) > 大失败(0)`、以及两边都失败时
    "技能值高者胜"，都会判出赢家。真实发生过：玩家 63/60 失败、对手 62/50
    也失败，系统记 won=True，给叙事的文本却是「失败 vs 失败 → 胜」——
    叙事模型看懂了矛盾、按"失败"写，它比代码判对了。
    """
    # 双方都失败（含一方大失败）→ 僵局，主动方没赢
    assert dice.resolve_opposed(_outcome(90, 70), _outcome(100, 70)) is False
    assert dice.resolve_opposed(_outcome(90, 70), _outcome(90, 70)) is False
    # 复现试玩里那一掷：63/60 失败 vs 62/50 失败
    assert dice.resolve_opposed(_outcome(63, 60), _outcome(62, 50)) is False
    # 主动方成功、对手失败 → 赢（技能值再低也赢）
    assert dice.resolve_opposed(_outcome(20, 30), _outcome(90, 80)) is True


# ── 2. 越界不夹紧 ───────────────────────────────────


async def _seed(room_code: str) -> tuple[str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="对抗房",
            max_players=4,
            phase="InGame",
            keeper_state={},
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="凌铭辉")
        db.add(player)
        await db.flush()
        db.add(
            Character(
                room_id=room.id,
                player_id=player.id,
                status="complete",
                name="凌铭辉",
                occupation="记者",
                age=30,
                gender="男",
                attributes={
                    "STR": 60,
                    "CON": 70,
                    "SIZ": 50,
                    "DEX": 70,
                    "APP": 50,
                    "INT": 80,
                    "POW": 50,
                    "EDU": 70,
                    "LUCK": 55,
                },  # fmt: skip
                derived_stats={"HP": 12, "MP": 10, "SAN": 50, "MOV": 8},
                skills={},
            )
        )
        await db.commit()
        return room.id, player.id


def _deps(room_id: str, player_id: str, seed: int = 0) -> KeeperDeps:
    return KeeperDeps(
        room_id=room_id,
        player_id=player_id,
        session_factory=_session_factory,
        module=_MODULE,
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
        rng=random.Random(seed),
    )


@pytest.mark.parametrize("bad_value", [0, 101, -5])
async def test_out_of_range_target_skips_the_check_instead_of_clamping(bad_value: int) -> None:
    """🔴 夹紧等于把裁决器写错的数字变成一个看似合理的规则结论。

    真正的风险是 COC6 式属性点没换算（POT 16 直接填 16）——那种情况这里挡不住
    （16 是合法百分位），但至少写出 0/101 这种明显错的时候不会被悄悄圆过去。
    """
    room_id, player_id = await _seed(f"OPP{bad_value:03d}")
    decision = KeeperDecision(
        thinking="抵抗毒烟",
        narration_guidance="写对抗",
        checks=[
            CheckRequest(
                skill_id="CON",
                player="凌铭辉",
                reason="抵抗毒烟",
                opposed=OpposedTarget(opponent="毒烟", value=bad_value),
            )
        ],
    )
    pending, issues = await create_pending_checks(_deps(room_id, player_id), decision)
    assert pending == []
    assert any("对抗检定" in i and str(bad_value) in i for i in issues)


async def test_valid_opposed_check_carries_opponent_into_pending() -> None:
    room_id, player_id = await _seed("OPP200")
    decision = KeeperDecision(
        thinking="抵抗毒烟",
        narration_guidance="写对抗",
        checks=[
            CheckRequest(
                skill_id="CON",
                player="凌铭辉",
                reason="抵抗毒烟",
                opposed=OpposedTarget(opponent="毒烟", value=80),
            )
        ],
    )
    pending, issues = await create_pending_checks(_deps(room_id, player_id), decision)
    assert issues == []
    assert len(pending) == 1
    assert pending[0].opposed_opponent == "毒烟"
    assert pending[0].opposed_value == 80


# ── 3. 结算 ─────────────────────────────────────────


async def test_opposed_roll_rolls_both_sides_and_reports_verdict() -> None:
    room_id, player_id = await _seed("OPP300")
    deps = _deps(room_id, player_id, seed=7)
    text, detail = await roll_check_detail(
        deps, "体质", "凌铭辉", opposed_opponent="毒烟", opposed_value=80
    )
    assert detail["target"] == 70  # CON 70
    assert detail["opposed_opponent"] == "毒烟"
    assert detail["opposed_target"] == 80
    assert 1 <= detail["opposed_rolled"] <= 100
    assert isinstance(detail["opposed_won"], bool)
    # 两边的骰子都要出现在给玩家看的文本里
    assert "毒烟" in text and str(detail["opposed_rolled"]) in text
    assert deps.check_results and "对抗" in deps.check_results[0]


async def test_plain_check_is_unchanged_when_no_opposed_given() -> None:
    """退化保证：不传 opposed 时 detail 里连键都不出现，文本也是老格式。"""
    room_id, player_id = await _seed("OPP400")
    deps = _deps(room_id, player_id, seed=7)
    text, detail = await roll_check_detail(deps, "体质", "凌铭辉")
    assert "opposed_opponent" not in detail
    assert "对抗" not in text
    assert "困难" in text  # 老格式里的难度换算提示
