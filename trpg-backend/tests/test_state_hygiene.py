"""对局状态的三处卫生问题（exec/19 #46 / #47 / #48，第二局试玩发现）。

26 轮跑完后 `keeper_state` 长这样：

    隐匿玩家: 8f0e1a44…          ← 第 6 轮躲进阴影，第 26 轮还挂着（#46）
    当前场景: 科比特家门外（警察到场）
    当前场景节点: basement-laboratory  ← 人在屋外，指针在地下室（#48）
    对局阶段: investigation           ← 结局叙事都写完了（#47）

三条的共同点：**状态该改的时候没人改**。#46/#48 是代码能确定性判断的，
收归代码；#47 是纯语义判断，只能把该判断的东西摆到裁决器眼前。
"""

import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7_content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.decision import KeeperDecision, PlayerMove, StateUpdate, StealthChange
from app.core.keeper.location_state import HIDDEN_PLAYERS_KEY, load_hidden_players
from app.core.keeper.module_loader import load_module
from app.core.keeper.phase import format_endings_status
from app.core.keeper.scene_state import CURRENT_NODE_KEY
from app.core.keeper.tools import KeeperDeps
from app.core.keeper.turn_executor import execute_side_effects
from app.models.room import Character, Player, Room

_FIXTURE_MODULE = str(Path(__file__).parent / "fixtures" / "keeper_module.json")
_MODULE = load_module(_FIXTURE_MODULE)

_db_path = Path(tempfile.mkdtemp(prefix="trpg-keeper-hygiene-test-")) / "hygiene.db"
_engine = create_async_engine(f"sqlite+aiosqlite:///{_db_path}", poolclass=NullPool)
_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _fresh_db():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(room_code: str, keeper_state: dict) -> tuple[KeeperDeps, str, str]:
    async with _session_factory() as db:
        room = Room(
            room_code=room_code,
            room_name="卫生房",
            max_players=4,
            phase="InGame",
            keeper_state=keeper_state,
        )
        db.add(room)
        await db.flush()
        a = Player(room_id=room.id, nickname="阿福")
        b = Player(room_id=room.id, nickname="阿贵")
        db.add_all([a, b])
        await db.flush()
        for p in (a, b):
            db.add(
                Character(
                    room_id=room.id,
                    player_id=p.id,
                    status="complete",
                    name=p.nickname,
                    occupation="记者",
                    age=30,
                    gender="男",
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
        deps = KeeperDeps(
            room_id=room.id,
            player_id=a.id,
            session_factory=_session_factory,
            module=_MODULE,
            ruleset=build_coc7_ruleset(),
        )
        return deps, a.id, b.id


async def _hide(room_id: str, *player_ids: str) -> None:
    """播种后才拿得到 player.id，所以隐匿状态单独写一次。"""
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        room.keeper_state = {
            **(room.keeper_state or {}),
            HIDDEN_PLAYERS_KEY: ", ".join(player_ids),
        }
        await db.commit()


async def _state(room_id: str) -> dict:
    async with _session_factory() as db:
        room = await db.get(Room, room_id)
        assert room is not None
        return dict(room.keeper_state or {})


# ── #46 隐匿：离开原地点自动解除 ─────────────────────


async def test_moving_to_a_new_node_clears_stealth() -> None:
    """🔴 试玩实测：第 6 轮躲进阴影，一路走到第 26 轮还挂着隐匿。

    多人局里这意味着队友**永远收不到他的消息**——投递见到 hidden 就只回给
    他本人。与 #37 同族：空间状态是地基。
    """
    deps, a_id, _b_id = await _seed("HYG001", {CURRENT_NODE_KEY: "hall"})
    await _hide(deps.room_id, a_id)
    await execute_side_effects(deps, KeeperDecision(current_node_id="cellar"))
    assert load_hidden_players(await _state(deps.room_id)) == set()


async def test_staying_in_the_same_node_keeps_stealth() -> None:
    """🔴 对照组：还在原地就还藏着——躲在阴影里观察是要能持续好几轮的，
    每轮都解除等于这个功能不存在。"""
    deps, a_id, _b_id = await _seed("HYG002", {CURRENT_NODE_KEY: "hall"})
    await _hide(deps.room_id, a_id)
    await execute_side_effects(deps, KeeperDecision(current_node_id="hall"))
    assert load_hidden_players(await _state(deps.room_id)) == {a_id}


async def test_move_player_also_clears_stealth_for_that_one() -> None:
    """分头移动走的是另一条路径（move_player_impl），同样要解除。"""
    deps, a_id, b_id = await _seed("HYG003", {CURRENT_NODE_KEY: "hall"})
    await _hide(deps.room_id, a_id, b_id)
    await execute_side_effects(
        deps, KeeperDecision(moves=[PlayerMove(player="阿贵", node_id="cellar")])
    )
    # 只有被挪走的阿贵解除，留在原地的阿福照旧藏着
    assert load_hidden_players(await _state(deps.room_id)) == {a_id}


async def test_explicit_stealth_change_still_works() -> None:
    """裁决器显式写 hidden=false 的路径不受影响（"被发现/主动现身"仍归它管）。"""
    deps, a_id, _b_id = await _seed("HYG004", {CURRENT_NODE_KEY: "hall"})
    await _hide(deps.room_id, a_id)
    await execute_side_effects(
        deps, KeeperDecision(stealth=[StealthChange(player="阿福", hidden=False)])
    )
    assert load_hidden_players(await _state(deps.room_id)) == set()


# ── #48 场景变了但没有对应节点 → 清空指针，不留旧值 ──────


async def test_scene_change_without_a_node_clears_the_pointer() -> None:
    """🔴 试玩实测：`当前场景=科比特家门外`，节点指针还停在 basement-laboratory，
    护栏于是拿地下室的 checks[] 去卡一个已经站在屋外的玩家。

    裁决器**做对了**（找不到对应节点就留空，不编造 id），错在代码把"没说"
    当成了"没变"。
    """
    deps, _a, _b = await _seed("HYG005", {CURRENT_NODE_KEY: "cellar"})
    await execute_side_effects(
        deps,
        KeeperDecision(state_updates=[StateUpdate(key="当前场景", value="镇上的警察局")]),
    )
    state = await _state(deps.room_id)
    assert CURRENT_NODE_KEY not in state
    assert state["当前场景"] == "镇上的警察局"


async def test_scene_change_with_a_node_keeps_the_pointer() -> None:
    """给了 node_id 就照常写——清空只发生在"换了场景且没有对应节点"时。"""
    deps, _a, _b = await _seed("HYG006", {CURRENT_NODE_KEY: "hall"})
    await execute_side_effects(
        deps,
        KeeperDecision(
            current_node_id="cellar",
            state_updates=[StateUpdate(key="当前场景", value="地下室")],
        ),
    )
    assert (await _state(deps.room_id))[CURRENT_NODE_KEY] == "cellar"


async def test_ordinary_turn_does_not_touch_the_pointer() -> None:
    """🔴 对照组：没提场景的普通轮次（对话、检定结算）绝不能动节点指针。

    没有这一条，"清空"会把每一个不写 current_node_id 的轮次都变成失位。
    """
    deps, _a, _b = await _seed("HYG007", {CURRENT_NODE_KEY: "hall"})
    await execute_side_effects(
        deps, KeeperDecision(state_updates=[StateUpdate(key="游戏内时间", value="第1天 深夜")])
    )
    assert (await _state(deps.room_id))[CURRENT_NODE_KEY] == "hall"


# ── #47 结局条件进局面块 ────────────────────────────


def test_endings_status_lists_every_ending_with_its_trigger() -> None:
    """结局条件此前只躺在 system prompt 末尾的剧本全文里；议程能被可靠触发，
    正是因为它每轮都以独立小节出现在局面块中。这里给结局同样的待遇。

    ⚠️ 如实说：这是概率性改进。"这段剧情算不算命中结局"是纯语义判断，
    没有代码手段能确定性判定。
    """
    text = format_endings_status(_MODULE)
    assert text  # fixture 模组有结局
    for ending in _MODULE.endings:
        assert ending.id in text
        assert ending.title in text


def test_endings_status_is_empty_for_a_module_without_endings() -> None:
    """没有结局的模组 → 空串 → 整块不渲染（退化保证）。"""
    stripped = _MODULE.model_copy(update={"endings": []})
    assert format_endings_status(stripped) == ""


# ── 代码记账的键不原样喂给模型（exec/27 阶段 3） ─────


def test_reserved_keys_are_the_single_source_of_what_the_model_may_not_write() -> None:
    """🔴 各能力声明的键必须真的进 `RESERVED_STATE_KEYS`。

    此前"不许写"和"不喂给模型"是两张手维护的清单，实测已经分叉：`NPC状态`
    两张都漏了。现在只有这一张，`agent` 直接引用它算 `visible_state`。
    """
    from app.core.keeper.capabilities import CAPABILITIES
    from app.core.keeper.tools import RESERVED_STATE_KEYS

    for capability in CAPABILITIES:
        for key in capability.reserved_state_keys:
            assert key in RESERVED_STATE_KEYS, f"{capability.name} 声明的 {key!r} 没进保留集合"


def test_the_model_never_sees_a_code_maintained_key_verbatim() -> None:
    """局面块的「世界状态笔记」里不许出现任何保留键。

    它们要么是机器格式（逐人位置是 `player_id@node_id`、隐匿玩家是 player id），
    要么已经由 situation 钩子渲染成人话——原样再喂一遍既是噪声也是泄漏。
    """
    from app.core.keeper.prompts import format_turn_input
    from app.core.keeper.tools import RESERVED_STATE_KEYS, visible_keeper_state

    keeper_state = dict.fromkeys(RESERVED_STATE_KEYS, "机器格式值")
    keeper_state["当前场景"] = "书房"
    # 🔴 过滤必须由**被测函数**做。第一版在这里自己写了一遍过滤，于是变异体
    # （把 visible_keeper_state 改成原样返回）照样绿——测试根本没走进被测代码。
    text = format_turn_input(visible_keeper_state(keeper_state), [], ["阿福"], "阿福", "我看看")
    assert "书房" in text
    for key in RESERVED_STATE_KEYS:
        assert key not in text


def test_empty_state_passes_through_untouched() -> None:
    from app.core.keeper.tools import visible_keeper_state

    assert visible_keeper_state(None) is None
    assert visible_keeper_state({}) == {}
