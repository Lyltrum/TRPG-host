"""模组标注的理智检定点：局面块 + 已触发记账（`exec/31 #73`）。

真人实测实据：导入的模组 23 个节点里只有 1 处 `kind="san"` 检定点，
玩家 04:09:35 **进了那个节点**（`keeper.node` 事件），当轮裁决只发了一次
逃跑的敏捷对抗——SAN 一次没起；全局唯一那次理智检定反而掷在没标注的节点上。

这里验证的是**提醒这一半**（触发条件由代码判）。掷不掷仍由模型决定，是
`exec/20` 里登记的概率性改进，不要在这里写"必掷"的断言——那会把一条
概率性改进伪装成保证。
"""

from __future__ import annotations

import random
import re
import tempfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.coc7.content import build_coc7_ruleset
from app.core.db import Base
from app.core.keeper.capabilities import reserved_state_keys, situation_blocks
from app.core.keeper.capabilities.san_check.executor import create_pending_san_checks
from app.core.keeper.capabilities.san_check.schema import SanCheckRequest
from app.core.keeper.capabilities.san_check.state import (
    SAN_POINTS_FIRED_KEY,
    format_recent_san,
    format_san_points,
    load_fired_san_points,
    load_recent_san_reasons,
    san_point_ref,
)
from app.core.keeper.capabilities.world_state.schema import StateUpdate
from app.core.keeper.contract.decision import KeeperDecision
from app.core.keeper.contract.module_loader import ScenarioModule
from app.core.keeper.contract.registry import PendingContext, SituationContext
from app.core.keeper.runtime.deps import KeeperDeps
from app.core.keeper.runtime.location_state import PLAYER_LOCATION_KEY
from app.core.keeper.runtime.scene_state import CURRENT_NODE_KEY, SCENE_NAME_KEY
from app.core.keeper.runtime.turn_executor import execute_side_effects
from app.models.event import Event
from app.models.room import Character, Player, Room

_MODULE = ScenarioModule.model_validate(
    {
        "meta": {"id": "san-points-fixture", "title": "试验模组"},
        "kp_truth": {"summary": "石堆后面蹲着的不是人。"},
        "player_intro": "你在林子里走。",
        "nodes": [
            {
                "id": "shrine",
                "title": "石堆神龛",
                "kp_text": "石堆后面蹲着那个东西。",
                "checks": [
                    {"skill": "侦察", "skill_ids": ["spot-hidden"], "kind": "skill"},
                    {
                        "skill": "理智",
                        "kind": "san",
                        "difficulty": "普通",
                        "on_success": "0",
                        "on_failure": "1D6",
                    },
                ],
            },
            {"id": "road", "title": "土路", "kp_text": "什么都没有。"},
        ],
    }
)


def _state(node: str | None = None, per_player: dict[str, str] | None = None) -> dict:
    out: dict = {}
    if node is not None:
        out[CURRENT_NODE_KEY] = node
    if per_player:
        out[PLAYER_LOCATION_KEY] = ", ".join(f"{p}@{n}" for p, n in per_player.items())
    return out


# ── 局面块 ──────────────────────────────────────────


# 🔴 **2026-08-15：注入口径从「玩家所在节点」改成「全模组」。**
#
# 08-14 回归实测量出来的：林中屋唯一那条 `kind="san"` 挂在 `migo-cover-blown`
# 上，那个节点 `exits: []` **且没有任何节点指向它**——图上的孤岛，玩家用任何
# 走法都站不上去。于是整块提示**一次都没渲染过**，28 轮 SAN 掷了 0 次。
# 不是模型不听话，是没给它看。同一个错误的第四张脸（前三次都在收尾门）：
# **判据用了一个不对应玩家实际处境的量**。
#
# 下面这几条原来断言的正是"按节点过滤"，随策略一起重写。


def test_the_block_lists_points_regardless_of_where_the_party_stands() -> None:
    """正题：站在没有标注的节点上，照样看得见全模组的标注。

    旧行为是这里返回空串——那正是 SAN 掷 0 次的直接原因。
    """
    text = format_san_points(_MODULE, _state(node="road"), (("p1", "阿福"),))
    assert "1D6" in text
    # 提醒必须说清"看见了才掷"——否则它会变成"进门就掷"，那是规则错的
    assert "目睹" in text and "还没看见" in text


def test_the_block_never_names_the_node() -> None:
    """🔴 **只给数值，不给标题**：标题本身就是剧透。

    遭遇类节点的标题往往把即将发生的事直接写在上面，光是列出来就等于提前
    告诉模型剧本要发生什么。夹具这条叫「石堆神龛」，同理不许出现。
    """
    for state in (_state(node="road"), _state(node="shrine"), {}):
        text = format_san_points(_MODULE, state, (("p1", "阿福"),))
        assert "石堆神龛" in _MODULE.nodes[0].title, "夹具前提：这个节点确实有标题"
        assert "石堆神龛" not in text, text


def test_a_fired_point_stops_being_advertised() -> None:
    """🔴 没有这笔记账，玩家会被提醒到这一局结束 → 重复扣 SAN。"""
    fired = {SAN_POINTS_FIRED_KEY: san_point_ref("shrine", 1)}
    assert format_san_points(_MODULE, fired, (("p1", "阿福"),)) == ""


def test_a_module_without_any_annotation_renders_nothing() -> None:
    """退化保证：整份模组一条 `kind="san"` 都没有 → 整块不渲染。

    这是"空块"唯一还成立的理由——**位置不再是理由了**。
    """
    bare = _MODULE.model_copy(
        update={"nodes": [n.model_copy(update={"checks": []}) for n in _MODULE.nodes]}
    )
    assert format_san_points(bare, _state(node="shrine"), (("p1", "阿福"),)) == ""


def test_being_outside_the_script_graph_no_longer_hides_the_reminder() -> None:
    """人在剧本节点之外（`exec/31 #72` 清空之后）也照样提醒。

    旧行为在这里返回空串。而"人不在剧本图上"恰恰是即兴段落，**更**需要提醒
    ——实测那一局玩家目击米-戈时正在林子里，位置指针指着一个没有标注的节点。
    """
    assert _state() == {}, "前提：keeper_state 里既没有房间指针也没有逐人位置"
    assert "1D6" in format_san_points(_MODULE, {}, (("p1", "阿福"),))


def test_registered_as_a_situation_block_and_a_reserved_key() -> None:
    blocks = situation_blocks(_MODULE, _state(node="shrine"))
    assert any("理智检定点" in body for _order, body in blocks)
    # 记账键是代码写的，state_updates 碰不到
    assert SAN_POINTS_FIRED_KEY in reserved_state_keys()


def test_the_narrator_never_sees_the_loss_dice() -> None:
    """🔴 `exec/23 #77`：叙事器读到这块，就把 `0/1D6` 念给了玩家听。

    这块整段都是**写给裁决器的指令**（"必须在 `san_checks` 里发起、损失表达式
    照抄下面的数值"），而局面块两阶段共用。**保密靠拿不到**——正解是不喂给
    叙事器，不是在叙事 prompt 里再加一条"别念机制"（那是 v1 已被推翻的做法）。
    """
    state = _state(node="shrine")
    keeper = situation_blocks(_MODULE, state)
    narrator = situation_blocks(_MODULE, state, keeper_view=False)

    assert any("理智检定点" in body for _order, body in keeper), "裁决器仍然必须看得见"
    assert not any("理智检定点" in body for _order, body in narrator)
    # 对照：光断言标题没了不够——损失骰本身一个字都不能出现在叙事器那份里
    narrator_text = "".join(body for _order, body in narrator)
    assert not re.search(r"\d*[dD]\d+", narrator_text), narrator_text


# ── 记账（走真实执行链）────────────────────────────

_db_path = Path(tempfile.mkdtemp(prefix="trpg-san-points-test-")) / "san.db"
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
async def solo() -> KeeperDeps:
    async with _session_factory() as db:
        room = Room(room_code="SAN001", room_name="理智房", max_players=4, phase="InGame")
        db.add(room)
        await db.flush()
        player = Player(room_id=room.id, nickname="阿福", is_host=True)
        db.add(player)
        await db.flush()
        db.add(
            Character(
                room_id=room.id,
                player_id=player.id,
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
                derived_stats={"HP": 10, "MP": 10, "SAN": 70, "MOV": 8},
                skills={"spot-hidden": 70},
            )
        )
        await db.commit()
        room_id, player_id = room.id, player.id

    return KeeperDeps(
        room_id=room_id,
        player_id=player_id,
        session_factory=_session_factory,
        module=_MODULE,
        ruleset=build_coc7_ruleset(),
        reserved_state_keys=reserved_state_keys(),
        turn_player_ids=(player_id,),
        rng=random.Random(7),
    )


async def _keeper_state(deps: KeeperDeps) -> dict:
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        return dict(room.keeper_state or {})


def _arrive_at(node_id: str, scene_name: str) -> KeeperDecision:
    """一次到达：节点指针 + 场景名一起写（规则 4 要求的就是这两件事一起做）。"""
    return KeeperDecision(
        current_node_id=node_id,
        state_updates=[StateUpdate(subject="world", key=SCENE_NAME_KEY, value=scene_name)],
    )


# 🔴 **记账口径必须跟着注入一起改**（2026-08-15）。
#
# 注入改成全局之后，原来那套「按玩家所在节点整节点标掉」几乎永远是空操作
# ——标注挂在遭遇节点上，玩家站不上去，于是标不掉、**提示每轮重复**，模型
# 照做就是重复扣 SAN，比不提醒更糟。
# 「加了字段没有消费方」是一种缺陷，**改了口径只改一半**是它的镜面版本，
# 两边都不会变红。改成按损失数值回匹（两侧同源：局面块原样列出、规则要求照抄）。


async def test_issuing_a_san_check_marks_the_matching_point(solo) -> None:
    deps = solo
    # 🔴 挪场景指针要**同时**声明新场景（2026-08-14 加的门）：规则原文要求的就是
    # 这两件事一起做，只写 node 是没有依据的改写——实测里指针被写回「调查起点」
    # 正是那个形状。这里照真实路径给两样。
    await execute_side_effects(deps, _arrive_at("shrine", "神龛"))
    decision = KeeperDecision(
        san_checks=[SanCheckRequest(loss_on_success="0", loss_on_failure="1D6", reason="目睹")]
    )
    _report, issues = await execute_side_effects(deps, decision)
    assert issues == []
    assert load_fired_san_points(await _keeper_state(deps)) == [san_point_ref("shrine", 1)]


async def test_the_point_is_marked_even_when_the_player_is_somewhere_else(solo) -> None:
    """🔴 正题：玩家**站在别处**照样标得掉。

    这正是旧口径做不到的那一半——实测里那条标注挂在玩家永远到不了的遭遇节点
    上，按位置记账等于永远标不掉。人在土路上目睹了那个东西，检定就该记在
    数值对得上的那条标注上。
    """
    deps = solo
    await execute_side_effects(deps, _arrive_at("road", "路上"))
    decision = KeeperDecision(
        san_checks=[SanCheckRequest(loss_on_success="0", loss_on_failure="1D6", reason="目睹")]
    )
    _report, issues = await execute_side_effects(deps, decision)
    assert issues == []
    assert load_fired_san_points(await _keeper_state(deps)) == [san_point_ref("shrine", 1)]


async def test_losses_that_match_nothing_are_not_marked_but_are_reported(solo) -> None:
    """数值对不上任何标注 → **不标**，并且说出来。

    在没标注的地方掷 SAN 完全合法（COC7 的基线表），那时没有检定点可记；
    但"记不上"必须是**显式**的，不能静默——静默的话，模型把数值抄错了也
    没人看得出来，那条标注会一直挂在提示里。
    """
    deps = solo
    await execute_side_effects(deps, _arrive_at("shrine", "神龛"))
    decision = KeeperDecision(
        san_checks=[SanCheckRequest(loss_on_success="0", loss_on_failure="1D3", reason="尸体")]
    )
    _report, issues = await execute_side_effects(deps, decision)
    assert load_fired_san_points(await _keeper_state(deps)) == []
    assert any("对不上模组标注" in issue for issue in issues), issues


async def test_case_differences_in_the_dice_expression_still_match(solo) -> None:
    """`1d6` 与 `1D6` 是同一个表达式。

    只归一大小写与空白——两侧同源，抄写时的抖动就这些。**不做同义词映射**。
    """
    deps = solo
    decision = KeeperDecision(
        san_checks=[SanCheckRequest(loss_on_success="0", loss_on_failure=" 1d6 ", reason="目睹")]
    )
    _report, issues = await execute_side_effects(deps, decision)
    assert issues == []
    assert load_fired_san_points(await _keeper_state(deps)) == [san_point_ref("shrine", 1)]


async def test_two_identical_checks_in_one_turn_only_consume_one_annotation(solo) -> None:
    """🔴 同一轮里两次同数值的检定，不该把同一条标注记两遍。

    没有"拿本轮已标的当基线"那一步，两次都会匹配到 `shrine#1`，`newly` 里
    出现两条一样的 ref。夹具只有一条标注，所以第二次应当匹配不上并报出来。
    """
    deps = solo
    same = SanCheckRequest(loss_on_success="0", loss_on_failure="1D6", reason="目睹")
    _report, issues = await execute_side_effects(deps, KeeperDecision(san_checks=[same, same]))
    assert load_fired_san_points(await _keeper_state(deps)) == [san_point_ref("shrine", 1)]
    assert len(issues) == 1, issues


# ── 一拍之内只掷一次理智（2026-08-16 真机，`exec/38 #86`）───────────


async def _submit(deps: KeeperDeps, text: str) -> None:
    """记一条玩家发言——「一拍」的起点就是它。"""
    async with _session_factory() as db:
        db.add(
            Event(
                room_id=deps.room_id,
                player_id=deps.player_id,
                event_type="action.submit",
                payload={"utterance": text},
            )
        )
        await db.commit()


async def _san_rolled(deps: KeeperDeps) -> None:
    """记一次已经掷出的理智检定（`san_check_only` 落的就是这个事件）。"""
    async with _session_factory() as db:
        db.add(
            Event(
                room_id=deps.room_id,
                player_id=deps.player_id,
                event_type="keeper.san",
                payload={"player": "阿福", "rolled": 8, "target": 70, "loss": 1, "san": 69},
            )
        )
        await db.commit()


async def _pending_san(deps: KeeperDeps) -> tuple[list, list[str]]:
    decision = KeeperDecision(
        san_checks=[SanCheckRequest(loss_on_success="0", loss_on_failure="1D6", reason="目睹")]
    )
    async with _session_factory() as db:
        return await create_pending_san_checks(
            deps, decision, PendingContext(db=db, keeper_state=None, current_scene=None)
        )


async def test_the_first_san_check_after_a_player_speaks_goes_through(solo) -> None:
    """一句话之后的**第一次**理智检定照常发起——门拦的不是"掷理智"。"""
    deps = solo
    await _submit(deps, "我打开手电筒看一下里面有什么")
    pending, issues = await _pending_san(deps)
    assert len(pending) == 1
    assert issues == []


async def test_a_second_san_check_in_the_same_beat_is_refused(solo) -> None:
    """🔴 **2026-08-16 真机的复现**：一句话引发了三次理智检定。

    玩家说完「我打开手电筒看一下里面有什么」之后**一个字都没再说**，系统跑了
    3 次裁决、3 次理智检定，第三次失败扣 6 点，**当场触发一次本不该有的临时性
    疯狂**。放大器是结算叙事——每掷完一批骰子就有一次结算叙事，而它本身又是
    一次完整裁决，可以再开新的 `san_checks`。

    规则 3 的「同一来源不重复检定」是**写着的**；根因是"已经为这个来源掷过了"
    这条信息从来没进过模型的上下文（模组标注的有记账，模型自判的记账是零）。

    **变异检验**：把 `create_pending_san_checks` 里那道 `san_already_rolled_
    this_beat` 判断删掉，这条当场红。
    """
    deps = solo
    await _submit(deps, "我打开手电筒看一下里面有什么")
    await _san_rolled(deps)  # 这一拍的第一次，已经掷完了

    pending, issues = await _pending_san(deps)
    assert pending == [], "同一拍的第二次理智检定必须被拦下"
    assert len(issues) == 1 and "已经掷过理智" in issues[0], issues


async def test_the_next_utterance_opens_a_new_beat(solo) -> None:
    """🔴 门必须有一条走得通的路：玩家再动一次就能再掷。

    没有这一条，这道门就从"不重复检定"变成"一局只掷一次理智"——那比原来的
    bug 更糟。真的情境升级了，下一拍照样掷得出来。
    """
    deps = solo
    await _submit(deps, "我打开手电筒看一下里面有什么")
    await _san_rolled(deps)
    assert (await _pending_san(deps))[0] == []

    await _submit(deps, "我后退一步，把门带上")
    pending, issues = await _pending_san(deps)
    assert len(pending) == 1, "新的一拍必须能重新掷"
    assert issues == []


async def test_two_sources_in_one_adjudication_still_roll_twice(solo) -> None:
    """拦的是**链条上的第二次裁决**，不是同一次裁决里的第二条。

    一进屋同时看见尸体和怪物是 COC 里正当的两次检定，别把它们也吞掉。
    """
    deps = solo
    await _submit(deps, "我推门进去")
    decision = KeeperDecision(
        san_checks=[
            SanCheckRequest(loss_on_success="0", loss_on_failure="1D3", reason="尸体"),
            SanCheckRequest(loss_on_success="1", loss_on_failure="1D6", reason="那个东西"),
        ]
    )
    async with _session_factory() as db:
        pending, issues = await create_pending_san_checks(
            deps, decision, PendingContext(db=db, keeper_state=None, current_scene=None)
        )
    assert len(pending) == 2, "同一次裁决里的多个来源各掷一次"
    assert issues == []


# ── 跨拍同源：记账走真实执行链（2026-08-18 真机）──────────────


async def _issue_san(deps: KeeperDeps, reason: str) -> tuple[list, list[str]]:
    decision = KeeperDecision(
        san_checks=[SanCheckRequest(loss_on_success="0", loss_on_failure="1D6", reason=reason)]
    )
    async with _session_factory() as db:
        return await create_pending_san_checks(
            deps, decision, PendingContext(db=db, keeper_state=None, current_scene=None)
        )


async def _keeper_state(deps: KeeperDeps) -> dict:
    async with _session_factory() as db:
        room = await db.get(Room, deps.room_id)
        assert room is not None
        return dict(room.keeper_state or {})


async def test_an_issued_san_check_is_remembered(solo) -> None:
    """🔴 **变异检验**：把 `create_pending_san_checks` 末尾那行
    `await _remember_san_reasons(...)` 删掉，这条当场红。"""
    deps = solo
    await _submit(deps, "我冲过去撞开那把椅子")
    await _issue_san(deps, "目睹那个中枪的人被近距离枪杀，脑浆溅到附近")

    assert load_recent_san_reasons(await _keeper_state(deps)) == [
        "目睹那个中枪的人被近距离枪杀，脑浆溅到附近"
    ]


async def test_a_refused_check_is_not_remembered(solo) -> None:
    """🔴 记的是**真正入队的**那些。

    被「一拍只掷一次」拦掉的请求玩家一眼都没看见，把它当成"已经掷过"会让
    下一拍的提醒指向一件没发生的事——那是「有消费方但没有数据」的镜面：
    有数据，但它描述的事没发生过。

    **变异检验**：把 `_remember_san_reasons` 的调用挪到那道门**之前**
    （即改成对 `decision.san_checks` 记账），这条当场红。
    """
    deps = solo
    await _submit(deps, "我打开手电筒看一下里面有什么")
    await _san_rolled(deps)  # 这一拍已经掷过了

    pending, issues = await _issue_san(deps, "这一次根本没发生")
    assert pending == [] and issues, "前提：这次必须真的被拦下"
    assert load_recent_san_reasons(await _keeper_state(deps)) == []


async def test_three_beats_on_one_corpse_are_all_visible_next_turn(solo) -> None:
    """🔴 **真机那三拍的原样复现**（2026-08-18）。

    连着三拍为同一具尸体掷了三次理智：被枪杀 → 爆头后复活起身 → 复活后蹒跚
    走向大门。三次各自跟在一句新的玩家发言后面，所以「一拍只掷一次」那道门
    一次都没拦——**它按拍分界，而这个病跨拍**。

    这里守的是：第四拍开始时，前三次的理由都摆在裁决器眼前。判不判仍归它
    （`state.py` 里写了为什么不拦）。
    """
    deps = solo
    reasons = (
        "目睹那个中枪的人被近距离枪杀，脑浆溅到附近",
        "目睹那个中枪的人被爆头后复活起身",
        "目睹那个中枪的人复活后蹒跚走向大门",
    )
    for i, reason in enumerate(reasons):
        await _submit(deps, f"第{i}句话")
        pending, _issues = await _issue_san(deps, reason)
        assert len(pending) == 1, f"第 {i} 拍是新的一拍，必须发得出去"

    state = await _keeper_state(deps)
    assert load_recent_san_reasons(state) == list(reasons)

    block = format_recent_san(SituationContext(module=_MODULE, keeper_state=state))
    for reason in reasons:
        assert reason in block
    assert "同一个来源不要重复检定" in block


async def test_the_narrator_never_sees_the_recent_san_block(solo) -> None:
    """跟检定点那块同样 `keeper_only`：这是写给裁决器的纪律。

    叙事器读到"最近为什么掷过理智"会把它当成可以复述的剧情——而那些理由里
    常常写着玩家还没确认的事（"被爆头后复活"）。
    """
    deps = solo
    await _submit(deps, "我看着他站起来")
    await _issue_san(deps, "目睹那个中枪的人被爆头后复活起身")
    state = await _keeper_state(deps)

    keeper = situation_blocks(_MODULE, state)
    narrator = situation_blocks(_MODULE, state, keeper_view=False)
    assert any("最近的理智检定" in body for _order, body in keeper)
    assert not any("最近的理智检定" in body for _order, body in narrator)
    assert not any("复活起身" in body for _order, body in narrator)
