"""movement：「说了场景没变，就不该同时挪节点指针」这道门的两档判定。

门本身是 2026-08-14 加的（实测十次错误全是同一个形状：场景名原样重写 +
节点改成别的）。**这个文件守的是 08-15 那次收紧**——原来只有"人站在即兴地点
上"才拦，剧本节点之间一律只 warn，理由是"两个节点可以同名，硬拦会误伤真实
移动"。回归实测又抓到两次，**两端标题都不一样**，那条豁免在标题不同时根本
不成立。

🔴 **为什么这道门值得从 warn 升成 block**：指针不是只影响显示，它是四条症状
的同一个根因——护栏按玩家所在节点取 `checks[]`、`format_san_points` 按玩家
所在节点注入理智检定点、顶栏位置提示直读指针、收尾门按节点数。指针错一次，
这四处一起错，而且**四处都不会变红**。
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys
from app.core.keeper.capabilities.movement.schema import HidingChange
from app.core.keeper.capabilities.skill_check.schema import CheckRequest
from app.core.keeper.capabilities.world_state.schema import StateUpdate
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.location_state import (
    HIDDEN_PLAYERS_KEY,
    IMPROVISED_LOCATION_KEY,
    PLAYER_LOCATION_KEY,
)
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY, SCENE_NAME_KEY
from app.core.keeper.runtime.turn_executor import execute_side_effects
from app.models.room import Character, Player, Room

_TESTS_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "trpg-backend") / "tests"
_MODULE = load_module(str(_TESTS_DIR / "fixtures" / "keeper_module.json"))

#: fixture 的四个节点标题两两不同（门厅 / 门厅脚印 / 地下室 / 暗格保险箱）。
#: 「两个节点同名」那一支在 fixture 里造不出来，所以单独派生一份模组，把
#: `cellar` 的标题改成跟 `hall` 一样——那正是当初发豁免的那种情形。
_SAME_TITLE_MODULE = _MODULE.model_copy(
    update={
        "nodes": [
            node.model_copy(update={"title": "门厅"}) if node.id == "cellar" else node
            for node in _MODULE.nodes
        ]
    }
)

_db_path = Path(tempfile.mkdtemp(prefix="trpg-repoint-test-")) / "repoint.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(room_code: str, keeper_state: dict) -> KeeperDeps:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="指针房",
            max_players=4,
            phase="InGame",
            keeper_state=keeper_state,
        )
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="阿福")
        db.add(player)
        await db.flush()
        db.add(
            Character(
                room_id=room.id,
                player_id=player.id,
                status="complete",
                name="阿福",
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
        )


async def _pointer(deps: KeeperDeps) -> str | None:
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        return (room.keeper_state or {}).get(CURRENT_NODE_KEY)


def _restate(scene: str, node_id: str) -> KeeperDecision:
    """场景名**原样重写**（值跟上一轮相同）+ 把指针改到别处。就是要防的那个形状。"""
    return KeeperDecision(
        state_updates=[StateUpdate(key=SCENE_NAME_KEY, value=scene)],
        current_node_id=node_id,
    )


# ── 08-15 收紧的那一档：两端标题不同 ⇒ 拦 ─────────────────


async def test_repoint_between_differently_titled_nodes_is_blocked() -> None:
    """回归实测形态之一：场景「度假屋卧室」原样重写，指针从卧室一跳到主卧。

    两端标题不同 ⇒ 不存在"移动到同名的另一处"这种解释 ⇒ 拦。
    """
    deps = await _seed("RPT001", {SCENE_NAME_KEY: "门厅", CURRENT_NODE_KEY: "hall"})
    _report, issues = await execute_side_effects(deps, _restate("门厅", "cellar"))

    assert await _pointer(deps) == "hall", "指针必须留在原地"
    assert any("场景定位未执行" in issue for issue in issues), issues


async def test_the_blocked_issue_names_the_node_it_refused() -> None:
    """issue 要说清拒绝的是哪个 id——排查时看的就是这个。"""
    deps = await _seed("RPT002", {SCENE_NAME_KEY: "门厅", CURRENT_NODE_KEY: "hall"})
    _report, issues = await execute_side_effects(deps, _restate("门厅", "cellar"))

    assert any("cellar" in issue for issue in issues), issues


# ── 已有的那一档：站在即兴地点上 ⇒ 拦（回归保护） ─────────────


async def test_repoint_from_an_improvised_location_is_still_blocked() -> None:
    """`loc-N` 的语义就是"剧本里没有这个地方"，挪到任何剧本节点都是错的。"""
    deps = await _seed(
        "RPT003",
        {
            SCENE_NAME_KEY: "温特公寓",
            CURRENT_NODE_KEY: "loc-1",
            IMPROVISED_LOCATION_KEY: {"loc-1": {"name": "温特公寓", "from": "hall"}},
        },
    )
    _report, issues = await execute_side_effects(deps, _restate("温特公寓", "hall"))

    assert await _pointer(deps) == "loc-1"
    assert any("场景定位未执行" in issue for issue in issues), issues


# ── 判不准的那一档：两端同名 ⇒ 报而不断 ────────────────────


async def test_repoint_between_same_titled_nodes_only_warns() -> None:
    """🔴 这一支**必须**留着：玩家真的可以移动到另一处也叫这个名字的地方。

    代码判得了触发条件（场景名没变）但判不准该不该拦，那就报而不断——同
    `_entity_name_in_key` 的先例。断言指针**真的改了**，不然这条豁免名存实亡。
    """
    deps = await _seed("RPT004", {SCENE_NAME_KEY: "门厅", CURRENT_NODE_KEY: "hall"})
    deps = replace(deps, module=_SAME_TITLE_MODULE)
    _report, issues = await execute_side_effects(deps, _restate("门厅", "cellar"))

    assert await _pointer(deps) == "cellar", "同名的两处之间是合法移动，不能拦"
    assert any("场景定位可疑" in issue for issue in issues), issues


async def test_an_unresolvable_target_degrades_to_warn_not_block() -> None:
    """取不到标题就不拦：那是**缺数据**，不是"标题不一样"的证据。

    显式降级，不拿"查不到"当"不相同"用（写进去会被 `set_current_node_impl`
    的存在性校验挡掉，那是另一道门的职责）。

    ⚠️ 断言只能盯**这道门自己那句**（含「跟上一轮相同」）：存在性校验拒绝时
    也发一条「场景定位未执行：没有 id=…」，按前缀扫会把两道门混成一道。
    """
    deps = await _seed("RPT005", {SCENE_NAME_KEY: "门厅", CURRENT_NODE_KEY: "hall"})
    _report, issues = await execute_side_effects(deps, _restate("门厅", "nonexistent-node"))

    gate_issues = [issue for issue in issues if "跟上一轮相同" in issue]
    assert gate_issues, issues
    assert all("场景定位可疑" in issue for issue in gate_issues), gate_issues


# ── 对照组：门不该在这些情形下开火 ──────────────────────


async def test_a_real_scene_change_is_not_touched_by_the_gate() -> None:
    """场景真的变了 ⇒ 这就是正常移动，指针照改、一条 issue 都不该有。"""
    deps = await _seed("RPT006", {SCENE_NAME_KEY: "门厅", CURRENT_NODE_KEY: "hall"})
    _report, issues = await execute_side_effects(
        deps,
        KeeperDecision(
            state_updates=[StateUpdate(key=SCENE_NAME_KEY, value="地下室")],
            current_node_id="cellar",
        ),
    )

    assert await _pointer(deps) == "cellar"
    assert not any("场景定位" in issue for issue in issues), issues


async def test_restating_the_scene_without_moving_the_pointer_is_fine() -> None:
    """原样重写场景、指针也没变 = 「我还在原地」，那是合法表达。"""
    deps = await _seed("RPT007", {SCENE_NAME_KEY: "门厅", CURRENT_NODE_KEY: "hall"})
    _report, issues = await execute_side_effects(deps, _restate("门厅", "hall"))

    assert await _pointer(deps) == "hall"
    assert not any("场景定位" in issue for issue in issues), issues


async def test_no_prior_pointer_means_nothing_to_contradict() -> None:
    """上一轮就没有指针 ⇒ 这一轮给一个不是"改写"，是第一次落位。"""
    deps = await _seed("RPT008", {SCENE_NAME_KEY: "门厅"})
    _report, issues = await execute_side_effects(deps, _restate("门厅", "cellar"))

    assert await _pointer(deps) == "cellar"
    assert not any("场景定位" in issue for issue in issues), issues


async def test_players_stay_where_they_were_when_the_repoint_is_blocked() -> None:
    """拦下来之后**玩家位置也不能跟着走**——门拦的是整个落位，不只是房间指针。"""
    deps = await _seed(
        "RPT009",
        {SCENE_NAME_KEY: "门厅", CURRENT_NODE_KEY: "hall"},
    )
    await execute_side_effects(deps, _restate("门厅", "cellar"))

    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        locations = (room.keeper_state or {}).get(PLAYER_LOCATION_KEY) or {}
    assert "cellar" not in str(locations), locations


# ── 本轮要掷潜行的人，「进入隐匿」不在 movement 生效 ────────────
#
# 🔴 回归实测：裁决同时写了 `checks:[stealth]` 和 `hiding:[{hidden:true}]`，
# 检定被护栏吞掉，隐匿状态照样落库——**藏起来是白给的**，两次都是。
# 病根是这两条路互不相干。现在 `skill_check`（order=5）把"本轮谁要掷潜行"
# 发布到 `TurnFacts`，movement 读到就把结论让给检定结算。


async def _hidden(deps: KeeperDeps) -> str:
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        return (room.keeper_state or {}).get(HIDDEN_PLAYERS_KEY) or ""


async def test_hiding_waits_for_the_stealth_roll_declared_in_the_same_turn() -> None:
    """同一轮里既要掷潜行又说藏起来了 ⇒ 状态不写，等骰子。"""
    deps = await _seed("RPT010", {})
    report, issues = await execute_side_effects(
        deps,
        KeeperDecision(
            checks=[CheckRequest(skill_id="stealth")],
            hiding=[HidingChange(player="阿福", hidden=True)],
        ),
    )

    assert await _hidden(deps) == "", "🔴 潜行还没掷，人就已经藏好了"
    assert any("等这一轮的潜行检定结果" in line for line in report), report
    assert not issues, issues


async def test_hiding_without_a_stealth_roll_still_applies_immediately() -> None:
    """🔴 对照组：没人要掷的时候，`hidden=true` 照旧立刻生效。

    没人看得见时躲起来本来就不必掷（真人桌同理），那是 KP 有权直接给的。
    没有这一条，把整个分支改成"一律不写"也会绿。
    """
    deps = await _seed("RPT011", {})
    await execute_side_effects(
        deps, KeeperDecision(hiding=[HidingChange(player="阿福", hidden=True)])
    )

    assert await _hidden(deps) != "", "没有检定的隐匿不该被延后"


async def test_coming_out_of_hiding_is_never_deferred() -> None:
    """🔴 现身/被发现是**无条件**的，不因为本轮碰巧掷了潜行就延后。

    同 `open_threads` 那条不对称：进入由代码定，结束必须走 schema 字段。
    """
    deps = await _seed("RPT012", {})
    await execute_side_effects(
        deps, KeeperDecision(hiding=[HidingChange(player="阿福", hidden=True)])
    )
    assert await _hidden(deps) != ""

    await execute_side_effects(
        deps,
        KeeperDecision(
            checks=[CheckRequest(skill_id="stealth")],
            hiding=[HidingChange(player="阿福", hidden=False)],
        ),
    )
    assert await _hidden(deps) == "", "🔴 现身被延后了"
