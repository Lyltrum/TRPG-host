"""NPC 自己掷骰（2026-08-14 实测）。

叙事写「州警扣下扳机」，掷骰卡片却是 **凌铭辉 · 射击：步枪/霰弹枪 5/42**
——玩家身上根本没有枪（`equipment` 全程是 null）。同一拍里两个说法。

根因在 schema：`CheckRequest` 当时**只有 `player`**，NPC 主动做的事没有任何
合法写法，模型只剩两条路——不掷，或者记在玩家头上。它选了后者。同
「schema 表达不了的东西会从叙事里漏出去」。

🔴 **不对称是用户拍板的**：名册里有数据卡的 NPC 用它自己的数值真掷；即兴造
出来的 NPC（那个州警不在名册里）没有数值就拒绝，由叙事直接裁定。不让裁决器
现编目标值——那等于把难度交给模型自己定。

🔴 **夹具的数据只有形状是真的，内容全是编的**——第三方模组的数据卡行不许进
git（版权红线）。形状是拿六份真实模组量出来的：属性点是整数、可掷项是自由
文本且写法极不统一，百分数**未必在开头**（`斗殴 50%`、`格斗 40%(困难20/极难8)`
这类占多数），也有干脆没有 % 的裸数字打头写法。

第一版的正则只认"开头的百分数"，那是照着**一个**样本调的：六份模组量下来
只覆盖 43%，而且我读的还是库里那份被归一过的数据。现在两档合起来覆盖
31 条可掷候选里的 28 条，漏的 3 条本来就该拒。
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys
from app.core.keeper.capabilities.skill_check.schema import CheckRequest
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.primitives.npcs import (
    npc_ability_names,
    npc_check_target,
    npc_display_name,
    resolve_npc_id,
    resolve_npc_ref,
)
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.turn_executor import create_pending_checks
from app.models.event import Event
from app.models.room import Character, Player, Room

_MODULE = ScenarioModule.model_validate(
    {
        "meta": {"id": "npc-check-fixture", "title": "试验模组"},
        "kp_truth": {"summary": "石堆后面蹲着的不是人。"},
        "player_intro": "你在林子里走。",
        "nodes": [{"id": "shrine", "title": "石堆神龛", "kp_text": "石堆后面。"}],
        "npcs": [
            {
                "id": "crawler-1",
                "name": "爬行者 #1",
                "role": "怪物",
                # 🔴 数据**形状**照着六份真实模组归纳，**内容全是编的**
                # ——第三方模组的数据卡行不许进 git（版权红线）。守的是形状：
                # % 在中间 / % 带难度换算 / 裸数字打头 / 全角括号 / 取不出的垃圾行。
                "stats": {
                    "STR": 15,
                    "DEX": 14,
                    "HP": 12,
                    "拍击": "斗殴 70%",
                    "喷吐": "40% (20/8), 伤害 1D8",
                    "缠绕": "55 1D6 目标被束缚",
                    "低语": "30（详见上文）",
                    "armor": "无",
                    "san_loss_on_sight": "1/1D6",
                    "备注": "没有任何数字的一行",
                },
            },
            {"id": "witness", "name": "目击者", "role": "当地居民", "stats": {}},
        ],
    }
)

_db_path = Path(tempfile.mkdtemp(prefix="trpg-npc-check-test-")) / "npc.db"
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
        room = Room(room_code="NPCK01", room_name="NPC房", max_players=4, phase="InGame")
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
                attributes={
                    "STR": 55,
                    "CON": 55,
                    "SIZ": 55,
                    "DEX": 55,
                    "APP": 55,
                    "INT": 55,
                    "POW": 55,
                    "EDU": 55,
                    "LUCK": 55,
                },  # fmt: skip
                derived_stats={"HP": 11, "MP": 11, "SAN": 55, "MOV": 8},
                skills={},
            )
        )
        await db.commit()
        return KeeperDeps(
            room_id=room.id,
            player_id=player.id,
            session_factory=_session_factory,
            module=_MODULE,
            ruleset=build_coc7_ruleset(),
            reserved_state_keys=reserved_state_keys(),
            rng=random.Random(7),
        )


async def _checks(deps: KeeperDeps) -> list[dict]:
    async with _session_factory() as db:
        rows = await db.execute(Event.__table__.select().where(Event.room_id == deps.room_id))
        return [dict(r.payload) for r in rows.fetchall() if r.event_type == "keeper.check"]


# ── 取目标值：三种攻击项写法 + 属性点 ─────────────────────


def test_a_percentage_anywhere_in_the_line_is_the_target() -> None:
    """🔴 百分数**不一定在开头**——六份模组里"在中间"才是多数写法。"""
    assert npc_check_target(_MODULE, "crawler-1", "拍击") == 70
    # 带难度换算的括号不能被当成目标值
    assert npc_check_target(_MODULE, "crawler-1", "喷吐") == 40


def test_a_leading_bare_number_also_counts() -> None:
    """没有 % 的写法：开头的裸数字 + 后面跟着空白或括号。"""
    assert npc_check_target(_MODULE, "crawler-1", "缠绕") == 55
    assert npc_check_target(_MODULE, "crawler-1", "低语") == 30


def test_other_axes_are_never_roll_targets() -> None:
    """🔴 护甲、目击理智损失不是可掷的目标值，也不该出现在候选里。

    `1/1D6` 那种写法如果被当成"取开头的 1"，NPC 就会拿 1% 去掷——荒唐但不会
    报错，正是最难发现的一类。
    """
    assert npc_check_target(_MODULE, "crawler-1", "armor") is None
    assert npc_check_target(_MODULE, "crawler-1", "san_loss_on_sight") is None
    assert "armor" not in npc_ability_names(_MODULE, "crawler-1")
    assert "san_loss_on_sight" not in npc_ability_names(_MODULE, "crawler-1")


def test_an_attribute_point_is_multiplied_by_five() -> None:
    """COC7：属性点 ×5 才是百分位。"""
    assert npc_check_target(_MODULE, "crawler-1", "STR") == 75
    assert npc_check_target(_MODULE, "crawler-1", "DEX") == 70


def test_a_line_without_any_number_yields_nothing() -> None:
    """🔴 取不出就返回 None，**不猜**。调用方据此拒绝整条检定。"""
    assert npc_check_target(_MODULE, "crawler-1", "备注") is None
    assert npc_check_target(_MODULE, "crawler-1", "不存在的项") is None
    assert npc_check_target(_MODULE, "witness", "拍击") is None


def test_the_ability_key_must_match_exactly() -> None:
    """键名一字不差——数据卡整段就在裁决器眼前，模糊匹配是同义词打地鼠的开始。"""
    assert npc_check_target(_MODULE, "crawler-1", " 拍击 ") is None
    assert npc_check_target(_MODULE, "crawler-1", "拍") is None


# ── 走完整条路：立刻掷、不进待掷队列 ────────────────────


async def test_a_roster_npc_rolls_immediately_and_creates_no_pending(deps: KeeperDeps) -> None:
    """🔴 **不进待掷队列**：州警开枪不该要玩家替他按一下掷骰按钮。"""
    pending, issues = await create_pending_checks(
        deps,
        KeeperDecision(
            checks=[CheckRequest(skill_id="fighting-brawl", npc="crawler-1", ability="拍击")]
        ),
    )

    assert pending == [], "NPC 掷骰不该产生待玩家决定的记录"
    assert issues == [], issues
    recorded = await _checks(deps)
    assert len(recorded) == 1
    assert recorded[0]["target"] == 70


async def test_the_recorded_check_says_which_npc_rolled(deps: KeeperDeps) -> None:
    """🔴 事件里 `npc` 是**单独一个键**，不复用 `player`。

    复用正是这次 bug 的形状：掷骰卡片按 `player` 渲染，于是界面上写着玩家在
    射击、叙事里扣扳机的是州警。一份数据扮演两个角色必出结构性 bug。
    """
    await create_pending_checks(
        deps,
        KeeperDecision(
            checks=[CheckRequest(skill_id="fighting-brawl", npc="crawler-1", ability="拍击")]
        ),
    )

    payload = (await _checks(deps))[0]
    assert payload["npc"] == "爬行者 #1"
    assert payload["skill"] == "拍击"
    assert "player" not in payload, "NPC 掷的骰不许挂在任何玩家名下"


async def test_the_narration_is_told_who_rolled(deps: KeeperDeps) -> None:
    """叙事那一拍要知道这是谁掷的，否则代码判了、故事里还是玩家在开枪。"""
    await create_pending_checks(
        deps,
        KeeperDecision(
            checks=[CheckRequest(skill_id="fighting-brawl", npc="crawler-1", ability="拍击")]
        ),
    )

    assert any("爬行者 #1" in line and "拍击" in line for line in deps.check_results)


# ── 三道拒绝 ────────────────────────────────────


async def test_an_improvised_npc_is_refused(deps: KeeperDeps) -> None:
    """🔴 正题的另一半：名册里没有的人（那个州警）**不掷**，由叙事裁定。"""
    _pending, issues = await create_pending_checks(
        deps,
        KeeperDecision(checks=[CheckRequest(skill_id="firearm-rifle", npc="州警", ability="射击")]),
    )

    assert await _checks(deps) == []
    assert any("剧本名册里没有" in issue for issue in issues), issues
    # 拒绝要**配一条走得通的修法**：告诉它这一下该怎么办
    assert any("由叙事直接裁定" in issue for issue in issues), issues


async def test_a_missing_ability_is_refused_and_lists_the_options(deps: KeeperDeps) -> None:
    """没说掷哪一项就拒——并且把数据卡上有哪些项列出来（配一条走得通的修法）。"""
    _pending, issues = await create_pending_checks(
        deps, KeeperDecision(checks=[CheckRequest(skill_id="fighting-brawl", npc="crawler-1")])
    )

    assert await _checks(deps) == []
    assert any("拍击" in issue for issue in issues), issues


async def test_an_unparsable_ability_is_refused_and_lists_the_options(deps: KeeperDeps) -> None:
    _pending, issues = await create_pending_checks(
        deps,
        KeeperDecision(
            checks=[CheckRequest(skill_id="fighting-brawl", npc="crawler-1", ability="备注")]
        ),
    )

    assert await _checks(deps) == []
    assert any("取不出" in issue and "拍击" in issue for issue in issues), issues


# ── 对照组：玩家那条路一点没变 ──────────────────────


async def test_a_player_check_still_goes_through_the_pending_queue(deps: KeeperDeps) -> None:
    """🔴 退化保证：没有这一条，把整个分支改成"一律立刻掷"也会绿。"""
    pending, issues = await create_pending_checks(
        deps, KeeperDecision(checks=[CheckRequest(skill_id="spot-hidden")])
    )

    assert len(pending) == 1, "玩家检定仍然要等他自己按掷骰"
    assert issues == []
    assert await _checks(deps) == [], "待掷阶段不该已经掷出结果"


# ── 形态寻址：两个粒度的分工（2026-08-15）─────────────────

_FORMS_MODULE = ScenarioModule.model_validate(
    {
        "meta": {"id": "forms-fixture", "title": "形态试验"},
        "kp_truth": {"summary": "容器与真·多形态长得一样。"},
        "player_intro": "开场。",
        "nodes": [{"id": "hall", "title": "门厅", "kp_text": "空的。"}],
        "npcs": [
            # 🔴 **容器**：本体没有数据卡，四个形态其实是四个不同的人。
            # 实测的 `main-npcs` 就是这个形状（导入管线把小节标题做成了 NPC）。
            {
                "id": "main-npcs",
                "name": "主要NPC",
                "forms": [
                    {"id": "alan-devereux", "name": "艾伦·德弗罗", "stats": {"STR": 10}},
                    {"id": "jeremy-winter", "name": "杰里米·温特博士", "stats": {"STR": 9}},
                ],
            },
            # 🔴 **真·多形态**：同一个人的另一面。本体有数据卡。
            {
                "id": "butler",
                "name": "管家",
                "stats": {"STR": 12, "HP": 11},
                "forms": [{"id": "butler-unmasked", "name": "被揭穿时", "stats": {"STR": 14}}],
            },
        ],
    }
)


def test_a_form_resolves_to_itself_for_cast_and_checks() -> None:
    """🔴 正题：`resolve_npc_ref` **不上溯本体**。

    实测里裁决写 `alan-devereux`，落库成了 `main-npcs`——「此刻台上是谁」
    于是永远只能答"主要NPC"，`cast` 这片能力在这个模组上等于没做。
    """
    assert resolve_npc_ref(_FORMS_MODULE, "alan-devereux") == "alan-devereux"
    assert resolve_npc_ref(_FORMS_MODULE, "艾伦·德弗罗") == "alan-devereux"
    # 本体自己仍然寻得到
    assert resolve_npc_ref(_FORMS_MODULE, "main-npcs") == "main-npcs"


def test_hp_still_resolves_to_the_individual() -> None:
    """🔴 对照组：血是**个体的**，仍然上溯本体。

    真·多形态的两种样子共用一条血——管家被揭穿之后不该凭空多出一条命。
    没有这一条，把两个函数合成一个也会绿。
    """
    assert resolve_npc_id(_FORMS_MODULE, "butler-unmasked") == "butler"
    assert resolve_npc_ref(_FORMS_MODULE, "butler-unmasked") == "butler-unmasked"


def test_a_form_uses_its_own_stat_card() -> None:
    """形态有自己的数据卡就用它自己的（被揭穿的管家力气更大）。"""
    assert npc_check_target(_FORMS_MODULE, "butler", "STR") == 60
    assert npc_check_target(_FORMS_MODULE, "butler-unmasked", "STR") == 70
    assert npc_check_target(_FORMS_MODULE, "alan-devereux", "STR") == 50


def test_a_form_has_a_display_name_of_its_own() -> None:
    """展示名要认形态，否则卡片/局面块上还是"主要NPC"。"""
    assert npc_display_name(_FORMS_MODULE, "alan-devereux") == "艾伦·德弗罗"
    assert npc_display_name(_FORMS_MODULE, "butler-unmasked") == "被揭穿时"
