"""closure：没有预设结局时的自然收尾（`exec/30 §10.4`）。

守三件事：
1. 计数口径——含**缺数据显式降级**（不许报 0）与**扁平遍历**（只数顶层节点会
   得出假数字）；
2. 门只数配对与一次性议程；**「没去过的地方」不在门里**，且有一条用例实证
   它永远见不了底（2026-08-13 那个 bug 的形状）；
3. 局面块自己要说清楚哪几行是门槛——尤其「无进展轮数」是相反的信号。
"""

from __future__ import annotations

import random
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys
from app.core.keeper.capabilities.closure.executor import execute_closure
from app.core.keeper.capabilities.closure.remaining import (
    STALL_PUSH_THRESHOLD,
    format_key_facts,
    format_remaining_content,
    unfired_agenda_count,
    unrevealed_pair_count,
    unvisited_node_count,
)
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import load_module, reachable_visibility_pairs
from app.core.keeper.contract.registry import TurnFacts
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.location_state import PLAYER_LOCATION_KEY
from app.core.keeper.runtime.phase import (
    PHASE_ENDING,
    PHASE_FINISHED,
    PHASE_KEY,
    load_phase,
)
from app.core.keeper.runtime.progress_state import (
    AGENDA_FIRED_KEY,
    CLUES_REVEALED_KEY,
    STALLED_TURNS_KEY,
    VISITED_NODES_KEY,
    load_stalled_turns,
    load_visited_nodes,
)
from app.models.event import Event
from app.models.room import Character, Player, Room

_TESTS_DIR = next(p for p in Path(__file__).resolve().parents if p.name == "trpg-backend") / "tests"
_MODULE = load_module(str(_TESTS_DIR / "fixtures" / "keeper_module.json"))

#: fixture 模组扁平展开是 4 个节点：hall / hall-footprints / cellar，
#: 外加挂在 `sub_node`（**单数**）下的 hidden-safe。
#: 🔴 写这条用例时我的探查脚本只遍历了 `sub_nodes`（复数），当场少数一个——
#: 树形结构有两个子节点字段，这就是"报数量前先确认口径"的现场。
_ALL_NODES = 4
_ONCE_AGENDA = "night-1-footprints"
#: fixture 有 2 条配对，但只有 1 条**玩家揭得开**：`pair-hall-mud` 的真相侧是
#: `cellar`（真节点），`pair-butler-faces` 的真相侧是 `butler-secret`（不是节点）。
#: 🔴 2026-08-14 起分母只数前者——真相侧不指向节点的在结构上永远揭不开，
#: 留在分母里就是「这道门永远过不去」（林中屋 6 条里有 3 条正是这样）。
_PAIRS = 2
_REACHABLE_PAIRS = 1

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-closure-test-")) / "closure.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(room_code: str, state: dict) -> tuple[str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="收尾房",
            max_players=4,
            phase="InGame",
            keeper_state=state,
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
                skills={},
            )
        )
        await db.commit()
        return room.id, player.id


def _deps(room_id: str, player_id: str) -> KeeperDeps:
    return KeeperDeps(
        room_id=room_id,
        player_id=player_id,
        session_factory=_session_factory,
        module=_MODULE,
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
        rng=random.Random(0),
    )


async def _phase_of(room_id: str) -> str | None:
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        return load_phase(room.keeper_state)


async def _state_of(room_id: str) -> dict:
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        return dict(room.keeper_state or {})


async def _write_state(room_id: str, patch: dict) -> None:
    """就地改几个键（模拟别的能力在这一轮之前写过东西）。"""
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        room.keeper_state = {**dict(room.keeper_state or {}), **patch}
        await db.commit()


# ── 计数口径 ─────────────────────────────────────────


def test_node_count_walks_the_whole_tree_not_just_the_top_level() -> None:
    """🔴 只数顶层会得出"这个模组只有 2 个 node"——我已经凭这种假数字下过大结论。"""
    assert unvisited_node_count(_MODULE, {}) == _ALL_NODES
    assert unvisited_node_count(_MODULE, {VISITED_NODES_KEY: "hall, hall-footprints"}) == 2


def test_only_once_agenda_counts_as_remaining_content() -> None:
    """`once=False` 的事件可以反复发生，"还没发生过"对它不成立——拿它当
    "内容没跑完"的证据会让对局**永远收不了尾**。"""
    assert unfired_agenda_count(_MODULE, {}) == 1  # 两条议程里只有一条是 once
    assert unfired_agenda_count(_MODULE, {AGENDA_FIRED_KEY: _ONCE_AGENDA}) == 0


def test_pair_count_reads_the_room_wide_ledger() -> None:
    assert unrevealed_pair_count(_MODULE, {}) == _REACHABLE_PAIRS
    assert unrevealed_pair_count(_MODULE, {CLUES_REVEALED_KEY: "pair-hall-mud@*"}) == 0


def test_pairs_whose_secret_side_is_not_a_node_stay_out_of_the_denominator() -> None:
    """🔴 2026-08-14 实测：林中屋 6 条配对里 3 条的真相侧是 **NPC id**
    （`mi-go-1/2/3`），玩家没有任何办法揭开它们——留在分母里，这道门在结构上
    永远过不去。判据是「发现一道门永远过不去时，先量一遍它的两个端点」。"""
    reachable = reachable_visibility_pairs(_MODULE)
    assert [p.id for p in reachable] == ["pair-hall-mud"]
    assert len(_MODULE.visibility_pairs) == _PAIRS  # 另一条还在，只是不进分母
    # 分子分母来自同一个集合：全揭开就是 0，不会卡在一个永远减不掉的余数上
    assert unrevealed_pair_count(_MODULE, {CLUES_REVEALED_KEY: "pair-hall-mud@*"}) == 0


def test_stalling_turns_into_a_hard_ask_once_it_crosses_the_threshold() -> None:
    """🔴 2026-08-14 实测：这个信号跑到 26 轮，而它**没有任何消费方**——
    只是局面块里一句陈述。症状是玩家连说四轮「继续走」，拿到四段越来越像的
    洞穴描写，最后两拍**逐字相同**。

    过阈值之后那一行要变成本轮的硬要求，并且要明确说这**不是**该收尾的信号
    （打转要推、玩完了才收——同一个数不能既当油门又当刹车）。
    """
    calm = format_remaining_content(_MODULE, {STALLED_TURNS_KEY: STALL_PUSH_THRESHOLD - 1})
    assert "这一轮必须让局面动起来" not in calm

    stuck = format_remaining_content(_MODULE, {STALLED_TURNS_KEY: STALL_PUSH_THRESHOLD})
    assert "这一轮必须让局面动起来" in stuck
    assert "让路到头" in stuck and "让事件闯进来" in stuck and "跳过过程" in stuck
    # 🔴 不能被读成"该收尾了"：两件相反的处境不共用一个信号
    assert "该收尾的信号" in stuck and "打转要推，不要收场" in stuck


def test_missing_data_degrades_explicitly_instead_of_reporting_zero() -> None:
    """🔴 导入的模组曾经根本不产 `visibility_pairs`，「未揭开」于是恒为 0——
    而 0 跟"全揭开了"长得一模一样，正好把收尾门推向放行。"""
    bare = _MODULE.model_copy(update={"visibility_pairs": [], "agenda": [], "nodes": []})
    assert unrevealed_pair_count(bare, {}) is None
    assert unfired_agenda_count(bare, {}) is None
    assert unvisited_node_count(bare, {}) is None
    # 🔴 行为变更（2026-08-12）：三样存量全缺时**不再整块省略**——停滞轮数跟
    # 模组有没有数据无关，而"什么都数不出来的模组"恰恰是最需要它的那一种。
    bare_text = format_remaining_content(bare, {})
    assert "没有玩家揭得开的配对数据" in bare_text
    assert "没有新进展" in bare_text
    # 只缺一样时那一行要说清楚缺了什么，不能写成 0
    no_pairs = _MODULE.model_copy(update={"visibility_pairs": []})
    text = format_remaining_content(no_pairs, {})
    assert "没有玩家揭得开的配对数据" in text
    assert "还剩 0" not in text


def test_the_block_says_which_lines_are_the_gate() -> None:
    """🔴 模型看到的是一串长得一样的数字，不写清楚哪几行算数，它只能自己猜。

    「没去过的地方」和「无进展轮数」必须当场声明不是收尾依据——后者尤其：它大
    说明这桌人在**打转**（该推），不是内容**跑完了**（该收），两件相反的事。
    """
    text = format_remaining_content(_MODULE, {})
    assert "收尾门槛" in text
    gate, reference = text.split("【下面两行只是参考，不是收尾依据】")
    assert "线索配对" in gate and "议程" in gate
    assert "没去过的地方" in reference and "没有新进展" in reference
    assert "天然到不了 0" in reference
    assert "该给推力" in reference


def test_key_facts_are_put_in_front_of_the_adjudicator() -> None:
    """核心真相已经在 system prompt 的剧本全文里了，这里是第二次摆——理由同
    `format_endings_status`：埋在几千字中间等于每轮指望模型自己想起来去翻。

    ⚠️ 概率性改进：key_facts 是自由文本，代码数不了"揭开了几条"，做不成门槛。
    """
    text = format_key_facts(_MODULE)
    for fact in _MODULE.kp_truth.key_facts:
        assert fact in text
    # 没有 key_facts 的模组整块省略，不渲染一个空标题
    assert (
        format_key_facts(
            _MODULE.model_copy(
                update={"kp_truth": _MODULE.kp_truth.model_copy(update={"key_facts": []})}
            )
        )
        == ""
    )


# ── 去过的节点 ───────────────────────────────────────


async def test_visits_are_recorded_from_the_location_table_each_turn() -> None:
    """🔴 回合级读位置表，不挂在每个写位置的函数上：位置有三条写入路径
    （`current_node_id` / `moves` / 即兴地点），逐个挂就会漏。"""
    room_id, player_id = await _seed("CLS100", {PLAYER_LOCATION_KEY: "p1@hall, p2@cellar"})
    report, issues = await execute_closure(_deps(room_id, player_id), KeeperDecision(), TurnFacts())
    assert issues == []
    assert any("hall" in line and "cellar" in line for line in report)
    assert set(load_visited_nodes(await _state_of(room_id))) == {"hall", "cellar"}


async def test_recording_the_same_place_twice_is_a_no_op() -> None:
    """幂等：没新去过的地方就不写库、也不给叙事塞一句废话。"""
    room_id, player_id = await _seed(
        "CLS200", {PLAYER_LOCATION_KEY: "p1@hall", VISITED_NODES_KEY: "hall"}
    )
    report, issues = await execute_closure(_deps(room_id, player_id), KeeperDecision(), TurnFacts())
    assert issues == []
    assert report == []


# ── 无进展轮数 ───────────────────────────────────────


async def _utterance(room_id: str, text: str) -> None:
    """开新的一拍。「一拍」= 最后一条 `action.submit` 之后（`runtime/beat.py`）。"""
    async with _session_factory() as db:
        db.add(Event(room_id=room_id, event_type="action.submit", payload={"utterance": text}))
        await db.commit()


async def test_stalled_turns_count_up_when_nothing_new_happens() -> None:
    """🔴 前三份记账全是存量（还剩多少），回答不了「是不是在原地打转」——
    而真人 KP 判断收尾时数的正是后者。玩家可以一直偏离主线，存量永远不见底。"""
    room_id, player_id = await _seed(
        "CLS800", {PLAYER_LOCATION_KEY: "p1@hall", VISITED_NODES_KEY: "hall"}
    )
    for expected in (1, 2, 3):
        await _utterance(room_id, f"我又站着不动第 {expected} 次")
        await execute_closure(_deps(room_id, player_id), KeeperDecision(), TurnFacts())
        assert load_stalled_turns(await _state_of(room_id)) == expected


async def test_one_beat_only_counts_once_however_many_adjudications_it_takes() -> None:
    """🔴 **2026-08-18 真机**：19 拍里这个数一度到 15。

    一次玩家发言会引发多次裁决——每掷完一批骰子就有一次结算叙事，而它本身
    又是一次完整裁决。原来每次执行都 +1，于是**每次检定把这个数推高 2**：
    越认真检定的局越像在原地打转，而这个信号的语义恰恰相反。

    **变异检验**：把 `_record_progress` 里那句 `elif not await
    happened_this_beat(...)` 改回 `else`，这条当场红（会数到 3）。
    """
    room_id, player_id = await _seed(
        "CLS801", {PLAYER_LOCATION_KEY: "p1@hall", VISITED_NODES_KEY: "hall"}
    )
    await _utterance(room_id, "我打开手电筒看一下里面有什么")
    for _ in range(3):  # 一拍里的三次裁决（首轮 + 两次结算叙事）
        await execute_closure(_deps(room_id, player_id), KeeperDecision(), TurnFacts())
    assert load_stalled_turns(await _state_of(room_id)) == 1

    await _utterance(room_id, "我再往里走两步")
    await execute_closure(_deps(room_id, player_id), KeeperDecision(), TurnFacts())
    assert load_stalled_turns(await _state_of(room_id)) == 2, "新的一拍要照常算"


async def test_the_world_moving_forward_counts_as_progress() -> None:
    """🔴 **2026-08-18 真机**：那一局目睹了枪杀、拿到主线线索、触发了绑架议程、
    开合了 4 条悬而未决，按「去新节点 / 揭新线索」两样的口径**一样都不算进展**。

    `world_advanced_this_turn` 由 `open_threads`(55)/`established`(56)/
    `agenda`(60) 发布——它们都在 `closure`(85) 之前跑完。

    **变异检验**：把 `advanced` 里的 `or world_advanced` 去掉，这条当场红。
    """
    room_id, player_id = await _seed(
        "CLS802",
        {PLAYER_LOCATION_KEY: "p1@hall", VISITED_NODES_KEY: "hall", STALLED_TURNS_KEY: "6"},
    )
    await _utterance(room_id, "我把日志交给警察")
    await execute_closure(
        _deps(room_id, player_id), KeeperDecision(), TurnFacts(world_advanced_this_turn=True)
    )
    assert load_stalled_turns(await _state_of(room_id)) == 0


async def test_going_somewhere_new_resets_the_stall_counter() -> None:
    room_id, player_id = await _seed(
        "CLS810",
        {PLAYER_LOCATION_KEY: "p1@hall", VISITED_NODES_KEY: "hall", STALLED_TURNS_KEY: "4"},
    )
    await execute_closure(_deps(room_id, player_id), KeeperDecision(), TurnFacts())
    assert load_stalled_turns(await _state_of(room_id)) == 5

    await _write_state(room_id, {PLAYER_LOCATION_KEY: "p1@cellar"})
    await execute_closure(_deps(room_id, player_id), KeeperDecision(), TurnFacts())
    assert load_stalled_turns(await _state_of(room_id)) == 0


async def test_revealing_a_clue_also_counts_as_progress() -> None:
    """进展的口径只有代码确定性可判的两样：去了新地方、揭开了新线索。"""
    room_id, player_id = await _seed(
        "CLS820",
        {PLAYER_LOCATION_KEY: "p1@hall", VISITED_NODES_KEY: "hall", STALLED_TURNS_KEY: "6"},
    )
    facts = TurnFacts(clues_revealed_this_turn=True)
    await execute_closure(_deps(room_id, player_id), KeeperDecision(), facts)
    assert load_stalled_turns(await _state_of(room_id)) == 0


# ── 反向门 ───────────────────────────────────────────


def _done_state(**extra: str) -> dict:
    """内容跑完的状态：配对全揭、一次性议程已触发、三个节点都去过。"""
    return {
        PLAYER_LOCATION_KEY: "p1@cellar",
        CLUES_REVEALED_KEY: "pair-butler-faces@*, pair-hall-mud@*",
        AGENDA_FIRED_KEY: _ONCE_AGENDA,
        VISITED_NODES_KEY: "hall, hall-footprints, cellar, hidden-safe",
        **extra,
    }


async def test_content_exhausted_closes_the_story_without_inventing_an_ending_id() -> None:
    """🔴 行为变更（2026-08-12）：终点是 `ending` 而不是 `finished`。

    自然收尾纯属 KP 判断，所以给它一个**可撤回**的中间态；直达 `finished`
    的只有命中剧本预设结局那条路。
    """
    room_id, player_id = await _seed("CLS300", _done_state())
    report, issues = await execute_closure(
        _deps(room_id, player_id), KeeperDecision(story_ran_its_course=True), TurnFacts()
    )
    assert issues == []
    assert any("进入收尾" in line for line in report)
    assert await _phase_of(room_id) == PHASE_ENDING
    # 🔴 不许凭空造一个剧本里不存在的结局 id
    from app.core.keeper.runtime.phase import ENDING_ID_KEY

    assert (await _state_of(room_id)).get(ENDING_ID_KEY) in (None, "")


async def test_a_turn_that_just_revealed_a_clue_may_not_close() -> None:
    """还在往外掏线索的那一轮，故事显然还在往下走。"""
    room_id, player_id = await _seed("CLS400", _done_state())
    facts = TurnFacts(clues_revealed_this_turn=True)
    _report, issues = await execute_closure(
        _deps(room_id, player_id), KeeperDecision(story_ran_its_course=True), facts
    )
    assert any("刚揭开新线索" in i for i in issues)
    assert await _phase_of(room_id) != PHASE_FINISHED


async def test_an_untriggered_once_agenda_blocks_the_close() -> None:
    """一次性议程还没发生 = 剧本还有一整块内容没上桌，这时候收尾是明显还没完。"""
    room_id, player_id = await _seed("CLS500", _done_state(**{AGENDA_FIRED_KEY: ""}))
    _report, issues = await execute_closure(
        _deps(room_id, player_id), KeeperDecision(story_ran_its_course=True), TurnFacts()
    )
    assert any("议程" in i for i in issues)
    assert await _phase_of(room_id) != PHASE_ENDING


async def test_unrevealed_pairs_block_the_close() -> None:
    """门的另一半：配对**全部**揭开才准收，不设比例阈值（拍出来的阈值永远调不完）。"""
    room_id, player_id = await _seed("CLS510", _done_state(**{CLUES_REVEALED_KEY: ""}))
    _report, issues = await execute_closure(
        _deps(room_id, player_id), KeeperDecision(story_ran_its_course=True), TurnFacts()
    )
    assert any("配对没揭开" in i for i in issues)
    assert await _phase_of(room_id) != PHASE_ENDING


async def test_a_pair_nobody_can_reach_does_not_block_the_close() -> None:
    """🔴 2026-08-14：`pair-butler-faces` 的真相侧不是节点，玩家永远揭不开它。
    唯一揭得开的那条揭开之后，门就该放行——否则这道门在结构上过不去。

    这正是林中屋那一局的形状：6 条配对里 3 条指向 NPC id，收尾因此从来不可能
    触发（整局 106 次裁决 `clues_revealed` 一次没写过）。
    """
    room_id, player_id = await _seed(
        "CLS511", _done_state(**{CLUES_REVEALED_KEY: "pair-hall-mud@*"})
    )
    _report, issues = await execute_closure(
        _deps(room_id, player_id), KeeperDecision(story_ran_its_course=True), TurnFacts()
    )
    assert not any("配对没揭开" in i for i in issues)
    assert await _phase_of(room_id) == PHASE_ENDING


async def test_missing_pair_data_does_not_block_the_close() -> None:
    """🔴 缺数据显式降级，不当"还剩很多"用：`None` 是"这份模组数不出来"。

    反过来当门用的话，不产 `visibility_pairs` 的模组就又一次永远收不了尾——
    正是这条能力被真人打回来的那个形状。
    """
    bare = _MODULE.model_copy(update={"visibility_pairs": [], "agenda": []})
    room_id, player_id = await _seed("CLS520", _done_state(**{CLUES_REVEALED_KEY: ""}))
    _report, issues = await execute_closure(
        replace(_deps(room_id, player_id), module=bare),
        KeeperDecision(story_ran_its_course=True),
        TurnFacts(),
    )
    assert issues == []
    assert await _phase_of(room_id) == PHASE_ENDING


async def test_places_never_visited_do_not_block_the_close() -> None:
    """🔴 2026-08-13 回归：「没去过的地方」**不在门里**。

    它当过门槛，而它的分母是扁平展开的全部节点、玩家位置却只落在地点类节点上
    ⇒ 那个数永远见不了底 ⇒ 开放式模组永远等不到落幕。见下一条用例的实证。
    """
    room_id, player_id = await _seed("CLS530", _done_state(**{VISITED_NODES_KEY: ""}))
    _report, issues = await execute_closure(
        _deps(room_id, player_id), KeeperDecision(story_ran_its_course=True), TurnFacts()
    )
    assert issues == []
    assert await _phase_of(room_id) == PHASE_ENDING


def test_the_unvisited_count_can_never_reach_zero() -> None:
    """🔴 这条就是那个 bug 的实证：**走遍每一个地点，这个数照样不为 0**。

    玩家位置只可能落在地点类节点上（位置表写的就是这些），而分母是扁平展开的
    全部节点——fixture 4 个节点里只有 2 个 location，剩下的 clue / 无 kind 的
    子节点没有任何路径把它们标成"去过"。林中屋是 23 : 14 的同一形状。

    所以旧的「三个数都见底才准收」在**结构上**不可能成立，跟模型聪不聪明无关。
    发现一道门永远过不去时，先量它的两个端点，再决定是拆门还是修数。
    """
    from app.core.keeper.contract.module_loader import iter_all_nodes

    every_place = [n.id for n in iter_all_nodes(_MODULE.nodes) if n.kind == "location"]
    assert every_place, "fixture 得有地点，否则这条用例在测空气"
    remaining = unvisited_node_count(_MODULE, {VISITED_NODES_KEY: ", ".join(every_place)})
    assert remaining is not None and remaining > 0


async def test_a_scripted_ending_this_turn_wins_and_is_not_closed_twice() -> None:
    """progression（order=80）先跑。它已经按剧本结局收束过了就不重复收——
    否则剧本自己的落幕会被一个无 ending_id 的收尾覆盖掉。"""
    room_id, player_id = await _seed("CLS600", _done_state(**{PHASE_KEY: PHASE_FINISHED}))
    report, issues = await execute_closure(
        _deps(room_id, player_id), KeeperDecision(story_ran_its_course=True), TurnFacts()
    )
    assert issues == []
    assert not any("自然收尾" in line for line in report)


async def test_not_writing_the_field_never_closes_anything() -> None:
    """退化保证：字段是 false 时这片能力只记账，不碰阶段。"""
    room_id, player_id = await _seed("CLS700", _done_state())
    _report, issues = await execute_closure(
        _deps(room_id, player_id), KeeperDecision(), TurnFacts()
    )
    assert issues == []
    assert await _phase_of(room_id) != PHASE_FINISHED
