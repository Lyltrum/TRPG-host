"""per-player 位置（exec/14 P5.2a）：编解码 + 分组 + 写入侧 + 检定护栏按人判定。

fixture 沿用原创迷你庄园失窃案（tests/fixtures/keeper_module.json），
与任何第三方模组原文无关。
"""

import random
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys, situation_blocks
from app.core.keeper.capabilities.movement.schema import HidingChange, NewLocation, PlayerMove
from app.core.keeper.capabilities.movement.situation import render_improvised_locations
from app.core.keeper.capabilities.world_state.executor import update_state_impl
from app.core.keeper.capabilities.world_state.schema import StateUpdate
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import load_module
from app.core.keeper.contract.registry import SituationContext
from app.core.keeper.runtime.deps import KeeperDeps, KeeperToolError
from app.core.keeper.runtime.location_state import (
    IMPROVISED_SOFT_LIMIT,
    PLAYER_LOCATION_KEY,
    format_party_locations,
    group_players,
    is_party_split,
    load_hidden_players,
    load_improvised_locations,
    load_player_locations,
    location_of,
    resolve_content_node_id,
    serialize_player_locations,
)
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY
from app.core.keeper.runtime.turn_executor import create_pending_checks, execute_side_effects
from app.models.room import Character, Player, Room

_FIXTURE_MODULE = Path(__file__).parent / "fixtures" / "keeper_module.json"

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-location-test-")) / "location.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _character(room_id: str, player_id: str, name: str) -> Character:
    return Character(
        room_id=room_id,
        player_id=player_id,
        status="complete",
        name=name,
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
        skills={"spot-hidden": 70, "library-use": 60},
    )


@pytest.fixture
async def party() -> tuple[KeeperDeps, str, str]:
    """两人房：阿福（本轮发起者）+ 阿贵。返回 (deps, 阿福 id, 阿贵 id)。"""
    async with _session_factory() as db:
        room = Room(room_code="LOC001", room_name="分头房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        a = Player(room_id=room.id, nickname="阿福", is_host=True)
        b = Player(room_id=room.id, nickname="阿贵")
        db.add_all([a, b])
        await db.flush()
        db.add_all([_character(room.id, a.id, "侦探福"), _character(room.id, b.id, "记者贵")])
        await db.commit()
        room_id, a_id, b_id = room.id, a.id, b.id

    deps = KeeperDeps(
        room_id=room_id,
        player_id=a_id,
        session_factory=_session_factory,
        module=load_module(_FIXTURE_MODULE),
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
        turn_player_ids=(a_id,),
        rng=random.Random(42),
    )
    return deps, a_id, b_id


async def _state(deps: KeeperDeps) -> dict:
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        return dict(room.keeper_state or {})


# ── 1. 编解码 ───────────────────────────────────────


def test_roundtrip_and_ignores_garbage() -> None:
    assert load_player_locations({PLAYER_LOCATION_KEY: "p1@hall, p2@cellar"}) == {
        "p1": "hall",
        "p2": "cellar",
    }
    # 缺 @ / 空段 / 空值一律丢弃，不产生半条记录
    assert load_player_locations({PLAYER_LOCATION_KEY: "p1@hall, , broken, p3@"}) == {"p1": "hall"}
    assert load_player_locations(None) == {}
    assert serialize_player_locations({"p1": "hall"}) == "p1@hall"


def test_location_falls_back_to_room_pointer() -> None:
    """没被单独定位过的人 = 跟大部队在一起（有定义的默认值，不是静默兜底）。"""
    state = {CURRENT_NODE_KEY: "hall", PLAYER_LOCATION_KEY: "p2@cellar"}
    assert location_of(state, "p1") == "hall"
    assert location_of(state, "p2") == "cellar"
    # 房间级指针也没有 → None，**不得**当成"跟谁都在一起"
    assert location_of({}, "p1") is None


def test_group_players_keeps_input_order_and_isolates_unknown() -> None:
    state = {CURRENT_NODE_KEY: "hall", PLAYER_LOCATION_KEY: "p2@cellar"}
    assert group_players(state, ["p1", "p2", "p3"], frozenset()) == [
        ("hall", ["p1", "p3"]),
        ("cellar", ["p2"]),
    ]
    # 位置全未知时是一组，不是三组
    assert group_players({}, ["p1", "p2"], frozenset()) == [(None, ["p1", "p2"])]
    assert is_party_split(state, ["p1", "p2"]) is True
    assert is_party_split({CURRENT_NODE_KEY: "hall"}, ["p1", "p2"]) is False


def test_format_party_locations_is_empty_when_together() -> None:
    """退化保证：未分头 → 空串 → 局面块整块不渲染，prompt 与 P5.2 之前一致。"""
    module = load_module(_FIXTURE_MODULE)
    together = {CURRENT_NODE_KEY: "hall"}
    assert format_party_locations(module, together, [("p1", "阿福"), ("p2", "阿贵")]) == ""

    split = {CURRENT_NODE_KEY: "hall", PLAYER_LOCATION_KEY: "p2@cellar"}
    text = format_party_locations(module, split, [("p1", "阿福"), ("p2", "阿贵")])
    assert "阿福" in text and "阿贵" in text and "cellar" in text


# ── 2. 写入侧 ───────────────────────────────────────


async def test_current_node_moves_everyone_standing_with_the_speaker(party) -> None:
    """🔴 跟你站在一起的人跟你一起走（exec/19 #37 真人实测打脸后改的）。

    最初只挪"本轮发言的人"，结果两人肩并肩站着也会被判成分头：
    先张家豪发言（他拿到显式条目），再凌铭辉发言（房间指针跟着变，但张家豪
    的旧条目不再回落）→ 叙事分段投递、一个人什么都收不到，连裁决器都读着
    错误的「各自所在」少发了检定。
    """
    deps, a_id, b_id = party
    # 第一轮：阿福发言。两人此刻都没有显式位置（都回落 None）→ 一起走。
    _report, issues = await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    assert issues == []
    assert load_player_locations(await _state(deps)) == {a_id: "hall", b_id: "hall"}

    # 第二轮：还是阿福发言。阿贵已有显式条目「hall」，与阿福同处 → 仍然一起走。
    await execute_side_effects(deps, KeeperDecision(current_node_id="cellar"))
    state = await _state(deps)
    assert load_player_locations(state) == {a_id: "cellar", b_id: "cellar"}
    assert state[CURRENT_NODE_KEY] == "cellar"
    assert is_party_split(state, [a_id, b_id]) is False


async def test_current_node_does_not_drag_along_someone_who_split_off(party) -> None:
    """真分头的人不该被隔空传送走——这是当初那条顾虑，它仍然成立。"""
    deps, a_id, b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    # 阿贵单独去地下室
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="cellar")])
    )
    # 阿福（在门厅）继续走动：阿贵不在他那儿，不跟着走
    await execute_side_effects(deps, KeeperDecision(current_node_id="hidden-safe"))
    state = await _state(deps)
    assert load_player_locations(state) == {a_id: "hidden-safe", b_id: "cellar"}
    assert is_party_split(state, [a_id, b_id]) is True


async def test_the_room_pointer_cannot_walk_someone_into_the_other_group(party) -> None:
    """🔴 exec/33 §10 #79，双人真机最强复现（2026-08-11）。

    实况：阿贵原话「我在门廊上待着不动，就看着正门」，而裁决器写下
    `current_node_id = basement-laboratory`——**阿福**所在的地下室。裁决器是把
    这个字段当"这一幕的镜头在哪"写的，执行侧却当"把发言者搬过去"。于是
    `玩家位置` 里两个人都进了地下室。

    那不是移动，**那是会合**，而会合只能由当事人确认（§5.2）。协议当时确实
    挂了卡、投递也没漏，但位置已经写进去了——护栏、局面块「各自所在」、下一轮
    叙事读的都是错的地方。
    """
    deps, a_id, b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿福", node_id="cellar")])
    )
    before = load_player_locations(await _state(deps))
    assert before == {a_id: "cellar", b_id: "hall"}

    # 阿贵这一轮发言（他在门厅、明说不动），裁决器却把镜头写成了阿福那边。
    deps.turn_player_ids = (b_id,)
    deps.player_id = b_id
    report, issues = await execute_side_effects(deps, KeeperDecision(current_node_id="cellar"))

    state = await _state(deps)
    assert load_player_locations(state) == before
    # 连房间指针也不动：`location_of` 会回落到它，只改指针照样能隔空并组。
    assert state[CURRENT_NODE_KEY] == "hall"
    assert is_party_split(state, [a_id, b_id]) is True
    # 拒绝要说出口，不是静默跳过。
    assert any("会合" in line for line in report)
    assert issues == []


async def test_the_room_pointer_still_moves_the_speaker_into_an_empty_place(party) -> None:
    """上一条不许伤到正常那一半：目标没别人时，分头中的人照常跟着指针走。

    真机里阿福正是这么一路 `loc-1 → basement-laboratory` 走进去的。
    """
    deps, a_id, b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="cellar")])
    )
    await execute_side_effects(deps, KeeperDecision(current_node_id="hidden-safe"))
    assert load_player_locations(await _state(deps)) == {a_id: "hidden-safe", b_id: "cellar"}


async def test_moves_override_current_node(party) -> None:
    """分头：大家进地下室，阿贵单独留在门厅。顺序必须是 current_node → moves。"""
    deps, a_id, b_id = party
    deps.turn_player_ids = (a_id, b_id)
    decision = KeeperDecision(
        current_node_id="cellar",
        moves=[PlayerMove(player="阿贵", node_id="hall")],
    )
    report, issues = await execute_side_effects(deps, decision)
    assert issues == []
    assert any("阿贵" in line for line in report)
    state = await _state(deps)
    assert load_player_locations(state) == {a_id: "cellar", b_id: "hall"}
    assert is_party_split(state, [a_id, b_id]) is True


async def test_a_bogus_node_id_clears_the_pointer_instead_of_keeping_a_lie(party) -> None:
    """🔴 exec/31 #72：主路失败必须落进兜底，不许保留旧值。

    真机三次全中——玩家说「去卡比家」（原文提到、没建节点），裁决器手上正好有
    那个人的 **NPC id**，就把它写进了 current_node_id。set 抛异常被记成 issue，
    而清空写在 `elif` 里，于是**永远轮不到**：指针停在旧节点，护栏拿错节点的
    检定表去卡玩家、分组也跟着错。
    """
    deps, a_id, b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))

    _report, issues = await execute_side_effects(
        deps,
        KeeperDecision(current_node_id="butler-public"),  # NPC id，不是节点 id
    )
    assert any("butler-public" in i for i in issues)
    state = await _state(deps)
    assert CURRENT_NODE_KEY not in state
    assert load_player_locations(state) == {}
    assert location_of(state, a_id) is None and location_of(state, b_id) is None


async def test_declaring_a_scene_with_no_node_id_also_clears(party) -> None:
    """exec/19 #48 原本那一支：换了场景但剧本里没有对应节点 → 承认不知道。"""
    deps, a_id, _b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    decision = KeeperDecision.model_validate(
        {"state_updates": [{"key": "当前场景", "value": "墓地石堆旁"}]}
    )
    await execute_side_effects(deps, decision)
    assert location_of(await _state(deps), a_id) is None


async def test_clearing_only_moves_the_speakers_not_the_whole_room(party) -> None:
    """🔴 一个人走出地图，不该把**所有人**的位置一起抹掉（2026-08-10 多人实测）。

    实测证据链：阿贵一个人说「我离开客厅去门厅」（门厅不是剧本节点）→ 清空 →
    在地下室的阿福也丢了位置 → `group_players` 判成全都不知道在哪 = 同一组 =
    **不再算分头** → 下一段本该只发给阿福的结算叙事广播给了全房间
    （事件表里那一行 audience 为空）。**分头状态被一次清空推平，而私密性正是
    从位置派生的。**
    """
    deps, a_id, b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="cellar")])
    )
    # 本轮发言的是阿福（fixture 的 turn_player_ids），他走到了图外
    decision = KeeperDecision.model_validate(
        {"state_updates": [{"key": "当前场景", "value": "屋后的小巷"}]}
    )
    await execute_side_effects(deps, decision)

    state = await _state(deps)
    assert location_of(state, a_id) is None, "走出去的人该被清掉"
    assert location_of(state, b_id) == "cellar", "🔴 留在地窖的人不该被连带清空"
    assert is_party_split(state, [a_id, b_id]) is True, "分头状态必须还在"


async def test_clearing_also_drops_stealth(party) -> None:
    """离开地点 → 解除隐匿（exec/19 #46）。清空也是"离开"，第一版漏了这一半。

    实测：阿贵藏在窗帘后，说「我离开客厅去门厅」，之后 `隐匿玩家` 还挂着他。
    """
    deps, a_id, _b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    await execute_side_effects(
        deps, KeeperDecision(hiding=[HidingChange(player="阿福", hidden=True)])
    )
    assert load_hidden_players(await _state(deps)) == {a_id}
    leaving = KeeperDecision.model_validate(
        {"state_updates": [{"key": "当前场景", "value": "屋后小巷"}]}
    )
    await execute_side_effects(deps, leaving)
    assert load_hidden_players(await _state(deps)) == set()


async def test_an_ordinary_turn_leaves_the_pointer_alone(party) -> None:
    """没提场景也没给节点的普通轮次（对话、检定结算）不该动指针。"""
    deps, a_id, _b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    await execute_side_effects(deps, KeeperDecision())
    assert location_of(await _state(deps), a_id) == "hall"


# ── 2b. 即兴地点（exec/32）──────────────────────────


async def test_a_new_location_gets_a_code_assigned_id_and_takes_the_party_there(party) -> None:
    """玩家去剧本图外的地方（「卡比家」）→ 建表、发 id、人挪过去。

    真机那一轮（`exec/31 #72`）裁决器把 NPC id 写进了 current_node_id，位置当场
    作废。有了这条路，"去一个新地方"才有得表达。
    """
    deps, a_id, b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    report, issues = await execute_side_effects(
        deps, KeeperDecision(new_location=NewLocation(name="卡比家", from_id="hall"))
    )
    assert issues == []
    state = await _state(deps)
    table = load_improvised_locations(state)
    assert list(table) == ["loc-1"]
    assert table["loc-1"] == {"name": "卡比家", "from": "hall"}
    # 建了就是发言者的落点，同处的人一起走（与剧本节点同一套语义）
    assert load_player_locations(state) == {a_id: "loc-1", b_id: "loc-1"}
    assert any("卡比家" in line for line in report)


async def test_an_improvised_location_is_a_legal_target_afterwards(party) -> None:
    """建过之后它就是合法 id：能当 current_node_id，也能当 moves 的目标。"""
    deps, a_id, b_id = party
    await execute_side_effects(deps, KeeperDecision(new_location=NewLocation(name="墓地")))
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="loc-1")])
    )
    state = await _state(deps)
    assert load_player_locations(state) == {a_id: "hall", b_id: "loc-1"}
    assert is_party_split(state, [a_id, b_id]) is True


async def test_two_people_at_two_off_map_places_are_not_one_group(party) -> None:
    """🔴 这才是做这件事的主要收益（exec/32 §5）。

    没有地点表时两个图外的人位置都是 None、被判成站在一起——分头投递失效，
    而"你不在场所以你不知道"正是从位置派生的。
    """
    deps, a_id, b_id = party
    await execute_side_effects(deps, KeeperDecision(new_location=NewLocation(name="卡比家")))
    await execute_side_effects(
        deps, KeeperDecision(new_location=NewLocation(name="墓地"), current_node_id="loc-1")
    )
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="loc-2")])
    )
    state = await _state(deps)
    assert group_players(state, [a_id, b_id], frozenset()) == [("loc-1", [a_id]), ("loc-2", [b_id])]
    # 「各自所在」要写得出名字，不能是「（位置未记录）」
    text = format_party_locations(deps.module, state, [(a_id, "阿福"), (b_id, "阿贵")])
    assert "卡比家" in text and "墓地" in text


async def test_ids_are_never_reused_and_names_are_not_deduped(party) -> None:
    """重名不去重（名字不是标识符），id 只增不复用。"""
    deps, _a_id, _b_id = party
    await execute_side_effects(deps, KeeperDecision(new_location=NewLocation(name="墓地")))
    await execute_side_effects(deps, KeeperDecision(new_location=NewLocation(name="墓地")))
    table = load_improvised_locations(await _state(deps))
    assert list(table) == ["loc-1", "loc-2"]


async def test_an_unresolvable_origin_is_dropped_not_stored(party) -> None:
    """来路解析不出就丢掉——存下来它就会被当成 id 用（自由文本当标识符）。"""
    deps, _a_id, _b_id = party
    await execute_side_effects(
        deps, KeeperDecision(new_location=NewLocation(name="卡比家", from_id="butler-public"))
    )
    assert load_improvised_locations(await _state(deps))["loc-1"]["from"] is None


async def test_every_known_location_stays_visible_to_the_model(party) -> None:
    """🔴 白名单闭环（exec/32 §7.2 与 §8）：建过的每一条都要出现在局面块里。

    一旦有人给渲染加"只显示最近 N 条"，模型就会重建同名地点、一个地方两个 id，
    而那时什么都不会变红——所以这条要有测试守着。
    """
    deps, _a_id, _b_id = party
    for i in range(IMPROVISED_SOFT_LIMIT + 2):
        await execute_side_effects(deps, KeeperDecision(new_location=NewLocation(name=f"地点{i}")))
    state = await _state(deps)
    blocks = situation_blocks(deps.module, state)
    rendered = "\n".join(body for _order, body in blocks)
    for loc_id in load_improvised_locations(state):
        assert loc_id in rendered


def test_no_improvised_location_renders_nothing() -> None:
    """退化保证：一局都没用到时，局面块与 exec/32 之前逐字一致。"""
    module = load_module(_FIXTURE_MODULE)
    assert render_improvised_locations(SituationContext(module, {CURRENT_NODE_KEY: "hall"})) == ""


async def test_a_solo_move_to_the_same_node_does_not_drag_the_others(party) -> None:
    """🔴 「我去地下室，你留在客厅」——被留下的人不许被拖走（2026-08-10 多人实测）。

    裁决器同时写了 `current_node_id=cellar` 和 `moves=[阿福→cellar]`
    （thinking 写着"处理分头"）。两个字段说的是同一次移动，`moves` 点了名，
    它更具体。此前 `current_node_id` 先执行，把同处的阿贵一起带进了地窖：
    叙事说他在客厅、位置说他在地窖。
    """
    deps, a_id, b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    decision = KeeperDecision(
        current_node_id="cellar",
        moves=[PlayerMove(player="阿福", node_id="cellar")],
    )
    _report, issues = await execute_side_effects(deps, decision)
    assert issues == []
    state = await _state(deps)
    assert location_of(state, a_id) == "cellar"
    assert location_of(state, b_id) == "hall", "🔴 被留下的人不该跟着走"
    assert is_party_split(state, [a_id, b_id]) is True


async def test_naming_the_others_means_take_them_along_not_leave_the_speaker(party) -> None:
    """🔴 「全队一起去」的真实写法：`node=X` + `moves=[**其他每个人**→X]`。

    这条是第一版消解规则当天被反噬的实测复现：那一版只看"目标节点相同"，
    于是这种写法被判成"只有他去"，`current_node_id` 被丢掉，**发言者反而被
    留在原地**（位置成了 None）。两种写法的区别只在**谁被点名**。
    """
    deps, a_id, b_id = party
    decision = KeeperDecision(
        current_node_id="hall",
        moves=[PlayerMove(player="阿贵", node_id="hall")],  # 点的是**别人**
    )
    _report, issues = await execute_side_effects(deps, decision)
    assert issues == []
    state = await _state(deps)
    assert location_of(state, a_id) == "hall", "🔴 发言者不能被留在原地"
    assert location_of(state, b_id) == "hall"
    assert is_party_split(state, [a_id, b_id]) is False


async def test_a_normal_group_move_still_takes_everyone(party) -> None:
    """退化保证：`moves` 没指向同一个节点时，#37 的"同处者跟随"照旧。"""
    deps, a_id, b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    await execute_side_effects(deps, KeeperDecision(current_node_id="cellar"))
    state = await _state(deps)
    assert location_of(state, a_id) == "cellar" and location_of(state, b_id) == "cellar"


async def test_move_with_unknown_node_or_player_is_issue_not_crash(party) -> None:
    deps, a_id, _b_id = party
    decision = KeeperDecision(
        moves=[
            PlayerMove(player="阿贵", node_id="no-such-node"),
            PlayerMove(player="查无此人", node_id="hall"),
        ]
    )
    _report, issues = await execute_side_effects(deps, decision)
    assert len(issues) == 2
    assert any("no-such-node" in i for i in issues)
    assert any("查无此人" in i for i in issues)
    assert load_player_locations(await _state(deps)) == {}


async def test_player_location_key_is_reserved_from_state_updates(party) -> None:
    """LLM 的 state_updates 改不动逐人位置表。"""
    deps, _a_id, _b_id = party
    with pytest.raises(KeeperToolError):
        await update_state_impl(deps, PLAYER_LOCATION_KEY, "任意@任意")


# ── 3. 检定护栏按人判定 ─────────────────────────────


async def test_check_guard_uses_each_players_own_location(party) -> None:
    """🔴 分头后不能用房间级指针去卡另一个人的检定。

    fixture 里 hall 标注了检定点 spot-hidden、cellar 没标注 checks（即兴层放行）。
    阿福在 cellar、阿贵在 hall：
    - 阿福掷「图书馆使用」→ 所在节点无 checks → 放行；
    - 若按房间级指针（hall）判定，这一条会被 hall 的 checks 否掉。
    """
    deps, a_id, b_id = party
    deps.turn_player_ids = (a_id,)
    await execute_side_effects(deps, KeeperDecision(current_node_id="cellar"))
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="hall")])
    )

    decision = KeeperDecision.model_validate(
        {
            "checks": [
                {"skill_id": "library-use", "player": "阿福"},
                {"skill_id": "library-use", "player": "阿贵"},
            ]
        }
    )
    pending, issues = await create_pending_checks(deps, decision)
    assert [p.player_id for p in pending] == [a_id]
    # 阿贵那条被他自己所在的门厅护栏否掉（门厅只标注了 spot-hidden）
    assert len(issues) == 1
    assert "library-use" in issues[0] and "门厅" in issues[0]


# ── 位置的两个角色：可见性单元 vs 内容单位（2026-08-10 多人验证跑） ──


async def test_only_the_named_go_to_the_new_place(party) -> None:
    """🔴 「我一个人绕到屋后，你守着门口」——`movers` 让新地点只带走点到的人。

    没有它的时候，新地点一律走 `current_node_id`，会带走此刻与发言者同处的
    所有人，于是**最常见的那类分头**（望风、绕后、断后）表达不出来：真机上
    裁决器 thinking 明写「分头行动」，两个人的位置却还是同一个 id。
    """
    deps, a_id, b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    await execute_side_effects(
        deps,
        KeeperDecision(new_location=NewLocation(name="屋后", from_id="hall", movers=["阿福"])),
    )
    state = await _state(deps)
    assert location_of(state, a_id) == "loc-1"
    assert location_of(state, b_id) == "hall", "🔴 没被点名的人不该跟着走"
    assert is_party_split(state, [a_id, b_id]) is True


async def test_movers_do_not_trigger_the_left_the_map_fallback(party) -> None:
    """🔴 用了 `movers` 就是"落点已经安排好了"，兜底不该再把别人清空（2026-08-11 真机）。

    真机原样：裁决器写 `new_location=科比特家屋后 + movers=[阿福]`（它做对了），
    同一轮还声明了新「当前场景」→ 兜底判成"走出剧本图" → **把留在原地的队友
    清成 None**；而 None 是个吸收态（`group_players` 判成同一组），两组当场并回
    一组，下一轮两人同时发言只产出一段，分头与并行一起失效。

    同族于消解那一支：**「落点已安排」是个逐个列出情况的地方，加一种就要加一条。**
    """
    deps, a_id, b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    decision = KeeperDecision(
        new_location=NewLocation(name="屋后", from_id="hall", movers=["阿福"]),
        state_updates=[StateUpdate(key="当前场景", value="屋后")],  # 触发兜底的那个信号
    )
    _report, issues = await execute_side_effects(deps, decision)
    assert issues == []
    state = await _state(deps)
    assert location_of(state, a_id) == "loc-1"
    assert location_of(state, b_id) == "hall", "🔴 留在原地的人被兜底清空了"
    assert is_party_split(state, [a_id, b_id]) is True


async def test_new_place_without_movers_still_takes_everyone(party) -> None:
    """退化保证：不写 `movers` 时行为与 exec/32 逐字一致（全队一起去）。"""
    deps, a_id, b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    await execute_side_effects(
        deps, KeeperDecision(new_location=NewLocation(name="卡比家", from_id="hall"))
    )
    state = await _state(deps)
    assert location_of(state, a_id) == location_of(state, b_id) == "loc-1"


async def test_a_derived_place_still_reads_the_module_node(party) -> None:
    """🔴 站在「屋后」的人读得到「门厅」的内容——即兴地点沿 `from` 上溯。

    在此之前 `from` 只在局面块里渲染成"从哪来的"，从没被消费过：即兴地点是
    内容盲区，护栏找不到节点就全部放行、线索也查不到。位置的两个角色
    （可见性单元 / 内容单位）粒度天生不同，靠这条链各取所需。
    """
    deps, a_id, _b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    await execute_side_effects(
        deps,
        KeeperDecision(new_location=NewLocation(name="屋后", from_id="hall", movers=["阿福"])),
    )
    state = await _state(deps)
    assert location_of(state, a_id) == "loc-1"
    assert resolve_content_node_id(deps.module, state, "loc-1") == "hall"
    # 跟剧本无关的地方（没有 from）仍然什么都读不到——显式降级，不是兜底
    await execute_side_effects(deps, KeeperDecision(new_location=NewLocation(name="镇外的路")))
    assert resolve_content_node_id(deps.module, await _state(deps), "loc-2") is None


async def test_a_split_that_did_not_take_leaves_a_trace(party) -> None:
    """🔴 保险丝：点名单独行动、结果全队还在一处 → 记 issue。

    两次真机分头失败时系统里没有任何东西会说一声，上一跑还因为"清空位置"的
    副作用给出了假的成功。它不是闸门（零命中不代表没问题），只是让这件事**可见**。
    """
    deps, _a_id, _b_id = party
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    # 裁决器想让阿福单独去，但目标就是大家所在的那个 id
    _report, issues = await execute_side_effects(
        deps,
        KeeperDecision(current_node_id="hall", moves=[PlayerMove(player="阿福", node_id="hall")]),
    )
    assert any("分头未成立" in issue for issue in issues)


async def test_everyone_together_is_not_a_failed_split(party) -> None:
    """退化保证：`node=X + moves=[其他每个人→X]` 是「全队一起去」的合法写法，
    不许被上面那条保险丝误报。"""
    deps, _a_id, _b_id = party
    _report, issues = await execute_side_effects(
        deps,
        KeeperDecision(current_node_id="hall", moves=[PlayerMove(player="阿贵", node_id="hall")]),
    )
    assert issues == []
