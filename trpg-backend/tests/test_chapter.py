"""分段摘要 L2（exec/14 P4.2）。

L1 保证"事实不丢"，L2 承担"记得大概就行"的那部分——跟谁翻过脸、许过什么诺、
哪扇门被撞坏了。判据：**"必须记住"的走 L1，"记得大概就行"的走 L2。**
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.db import Base
from app.core.keeper.memory.chapter import (
    CHAPTER_HARD_CEILING,
    CHAPTER_MAX_CHARS,
    MIN_LINES_PER_CHAPTER,
    MIN_TURNS_BETWEEN_CHAPTERS,
    Chapter,
    build_recap,
    load_chapters,
    record_chapter,
    render_chapters,
    should_summarize,
    split_history_for_chapters,
    turns_since_last_chapter,
    visible_chapters,
)
from app.core.keeper.memory.history import HISTORY_LIMIT, HistoryLine
from app.models.event import Event
from app.models.room import Player, Room


@pytest.fixture
async def room(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/chapter.db", poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as db:
        r = Room(room_code="CHAPT1", room_name="摘要房", max_players=4, phase="InGame")
        db.add(r)
        await db.flush()
        p = Player(room_id=r.id, nickname="调查员甲", is_host=True)
        db.add(p)
        await db.commit()
        ids = (r.id, p.id)
    yield factory, ids
    await engine.dispose()


async def _add_actions(factory, room_id: str, player_id: str, count: int) -> None:
    async with factory() as db:
        for i in range(count):
            db.add(
                Event(
                    room_id=room_id,
                    player_id=player_id,
                    event_type="action.submit",
                    payload={"utterance": f"第 {i} 轮"},
                )
            )
        await db.commit()


# ── 触发条件：交集 + 一条兜底 ────────────────────────────────


def test_scene_change_alone_is_not_enough() -> None:
    """只按场景切换会在玩家来回踱步时疯狂触发。"""
    assert should_summarize(scene_changed=True, turns_since_last=1) is False


def test_turns_alone_is_not_enough_below_the_ceiling() -> None:
    """只按轮数会把一段完整的戏拦腰截断。

    🔴 2026-08-16 收窄：原来断言的是 `turns=999` 也不摘，而那正是缺陷——
    不换场景就永远不摘。现在的规则是「**在兜底上限以内**，光有轮数不够」。
    """
    assert should_summarize(scene_changed=False, turns_since_last=CHAPTER_HARD_CEILING - 1) is False


def test_both_conditions_trigger() -> None:
    assert should_summarize(scene_changed=True, turns_since_last=MIN_TURNS_BETWEEN_CHAPTERS) is True


def test_the_ceiling_fires_even_without_a_scene_change() -> None:
    """🔴 兜底：待在同一个场景里太久也必须摘一次。

    没有这条时，地下室搜半小时、审一个 NPC 审二十轮都不会产生任何摘要，
    而那段剧情滚出 L3 之后**再也没人能重建它**——L1 能从 id 重渲、L3 原文
    还在库里，只有 L2 是一次性的。
    """
    assert should_summarize(scene_changed=False, turns_since_last=CHAPTER_HARD_CEILING) is True


def test_the_ceiling_is_derived_from_the_history_window() -> None:
    """🔴 兜底上限必须**显著小于 L3 能覆盖的拍数**，否则它兜不住底。

    而且要跟着 `HISTORY_LIMIT` 走：写死一个数的话，哪天有人调大/调小历史
    窗口，安全边际会静默失效——失效的表现是"某几段剧情没了"，不会有任何
    东西变红。
    """
    beats_covered = HISTORY_LIMIT / 2.5
    assert beats_covered / 2 > CHAPTER_HARD_CEILING, "余量不足两倍，兜底可能来不及"
    assert CHAPTER_HARD_CEILING > MIN_TURNS_BETWEEN_CHAPTERS, "兜底比常规间隔还小就没意义了"


# ── 计数：从上一段摘要之后算起 ───────────────────────────────


@pytest.mark.asyncio
async def test_turn_count_resets_after_each_chapter(room) -> None:
    factory, (room_id, player_id) = room
    await _add_actions(factory, room_id, player_id, 5)
    async with factory() as db:
        assert await turns_since_last_chapter(db, room_id=room_id) == 5

    async with factory() as db:
        await record_chapter(db, room_id=room_id, text="调查员搜完了门厅，去了地下室。")
        await db.commit()

    # 摘要之后重新计数，不该把摘要之前的轮次再算一遍
    async with factory() as db:
        assert await turns_since_last_chapter(db, room_id=room_id) == 0
    await _add_actions(factory, room_id, player_id, 3)
    async with factory() as db:
        assert await turns_since_last_chapter(db, room_id=room_id) == 3


# ── 存储与渲染 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chapters_are_ordered_and_unbounded(room) -> None:
    """不设 limit——与 L1 同理，必须活过 L3 的 200 条窗口。"""
    factory, (room_id, player_id) = room
    async with factory() as db:
        for i in range(3):
            await record_chapter(db, room_id=room_id, text=f"第 {i} 段")
        await db.commit()
    await _add_actions(factory, room_id, player_id, 300)

    async with factory() as db:
        chapters = await load_chapters(db, room_id=room_id)
    assert [c.text for c in chapters] == ["第 0 段", "第 1 段", "第 2 段"]


@pytest.mark.asyncio
async def test_overlong_summary_is_truncated(room) -> None:
    """摘要要长期常驻上下文，不能自己变成新的上下文负担。"""
    factory, (room_id, _player_id) = room
    async with factory() as db:
        await record_chapter(db, room_id=room_id, text="很长" * 200)
        await db.commit()
    async with factory() as db:
        assert len((await load_chapters(db, room_id=room_id))[0].text) <= CHAPTER_MAX_CHARS


@pytest.mark.asyncio
async def test_blank_summary_is_not_recorded(room) -> None:
    """LLM 返回空时不该留一条空摘要占位。"""
    factory, (room_id, _player_id) = room
    async with factory() as db:
        await record_chapter(db, room_id=room_id, text="   ")
        await db.commit()
    async with factory() as db:
        assert await load_chapters(db, room_id=room_id) == []


def test_empty_chapters_render_nothing() -> None:
    """退化证明：短模组没有摘要 → 整块省略，局面块不变脏。"""
    assert render_chapters([]) == ""


# ── 上集回顾 ─────────────────────────────────────────────────


def test_recap_combines_chapters_and_ledger() -> None:
    recap = build_recap(["去了地下室，撬开了木箱。"], "- 箱底压着一张船票")
    assert "地下室" in recap
    assert "船票" in recap


def test_recap_is_empty_when_nothing_happened_yet() -> None:
    assert build_recap([], "") == ""


# ── agent 接线：离线生成本身 ─────────────────────────────────


class _CountingClient:
    """假 LLM 客户端：记调用次数，返回固定摘要。"""

    def __init__(self) -> None:
        self.calls = 0
        self.chat = type("_Chat", (), {"completions": self})()

    async def create(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls += 1
        from dataclasses import dataclass

        @dataclass
        class _M:
            content: str

        @dataclass
        class _C:
            message: _M
            finish_reason: str = "stop"

        @dataclass
        class _R:
            choices: list

        return _R(choices=[_C(message=_M("调查员离开门厅，下到地下室。"))])


def _agent_with(factory, client):
    from app.core.coc7.content import build_coc7_ruleset
    from app.core.keeper.contract.module_loader import load_module
    from app.core.keeper.runtime.agent import KeeperAgent

    agent = KeeperAgent(
        api_key="fake",
        module=load_module(Path(__file__).parent / "fixtures" / "keeper_module.json"),
        ruleset=build_coc7_ruleset(),
        session_factory=factory,
    )
    agent._client = client
    return agent


@pytest.mark.asyncio
async def test_summary_is_skipped_when_too_few_turns(room) -> None:
    """轮数不够时连模型都不该调——省钱也省延迟。"""
    factory, (room_id, player_id) = room
    await _add_actions(factory, room_id, player_id, MIN_TURNS_BETWEEN_CHAPTERS - 1)
    client = _CountingClient()
    await _agent_with(factory, client)._summarize_chapter(room_id, [HistoryLine("阿福：我下楼")])
    assert client.calls == 0
    async with factory() as db:
        assert await load_chapters(db, room_id=room_id) == []


@pytest.mark.asyncio
async def test_summary_is_generated_and_stored_when_due(room) -> None:
    factory, (room_id, player_id) = room
    await _add_actions(factory, room_id, player_id, MIN_TURNS_BETWEEN_CHAPTERS)
    client = _CountingClient()
    await _agent_with(factory, client)._summarize_chapter(room_id, [HistoryLine("阿福：我下楼")])
    assert client.calls == 1
    async with factory() as db:
        assert [c.text for c in await load_chapters(db, room_id=room_id)] == [
            "调查员离开门厅，下到地下室。"
        ]


@pytest.mark.asyncio
async def test_summary_failure_never_breaks_the_turn(room) -> None:
    """它是记忆的锦上添花，不是主路径——LLM 炸了也只记日志。"""
    factory, (room_id, player_id) = room
    await _add_actions(factory, room_id, player_id, MIN_TURNS_BETWEEN_CHAPTERS)

    class _Boom(_CountingClient):
        async def create(self, **kwargs):  # noqa: ANN003, ANN201
            raise RuntimeError("模型挂了")

    await _agent_with(factory, _Boom())._summarize_chapter(room_id, [HistoryLine("阿福：我下楼")])
    async with factory() as db:
        assert await load_chapters(db, room_id=room_id) == []


@pytest.mark.asyncio
async def test_background_task_reference_is_retained(room) -> None:
    """asyncio 只持弱引用——不自己存一份，任务可能被 GC 提前回收。"""
    factory, (room_id, player_id) = room
    agent = _agent_with(factory, _CountingClient())
    agent._spawn_chapter_summary(room_id, [HistoryLine("阿福：我下楼")], frozenset())
    assert len(agent._background) == 1
    for task in list(agent._background):
        await task


# ── 受众（exec/14 P5.2d 的残留缺口，2026-08-11 补上）─────────


_HALL = frozenset({"p-hall"})
_CELLAR = frozenset({"p-cellar"})


def test_a_group_only_sees_public_chapters_and_its_own() -> None:
    """🔴 摘要常驻上下文，泄得比历史还久——地下室那段不许出现在门厅的上下文里。"""
    chapters = [
        Chapter("大家在门厅碰头。"),
        Chapter("地下室里有个木箱。", _CELLAR),
        Chapter("门厅的地毯下有血迹。", _HALL),
    ]
    assert visible_chapters(chapters, _HALL) == ["大家在门厅碰头。", "门厅的地毯下有血迹。"]
    assert visible_chapters(chapters, _CELLAR) == ["大家在门厅碰头。", "地下室里有个木箱。"]


def test_the_keeper_sees_everything() -> None:
    """守秘人对整局一致性负责，必须看得见全部（与历史行同一口径）。"""
    chapters = [Chapter("公开"), Chapter("私密", _CELLAR)]
    assert visible_chapters(chapters, None) == ["公开", "私密"]


def test_nobody_gets_only_the_public_ones() -> None:
    """空受众 = 没有人。`frozenset() <= x` 恒为真，不显式挡住就朝**泄密**方向失败。"""
    chapters = [Chapter("公开"), Chapter("私密", _CELLAR)]
    assert visible_chapters(chapters, frozenset()) == ["公开"]


async def test_audience_survives_a_round_trip_through_the_database(room) -> None:
    factory, (room_id, _player_id) = room
    async with factory() as db:
        await record_chapter(db, room_id=room_id, text="公开的一段")
        await record_chapter(db, room_id=room_id, text="地下室那段", audience=_CELLAR)
        await db.commit()
    async with factory() as db:
        chapters = await load_chapters(db, room_id=room_id)
    assert [(c.text, c.audience) for c in chapters] == [
        ("公开的一段", None),
        ("地下室那段", _CELLAR),
    ]


def test_undivided_history_still_produces_exactly_one_public_chapter() -> None:
    """退化保证：没分头时与本功能上线前逐字一致。"""
    lines = [HistoryLine("阿福：我下楼"), HistoryLine("守秘人：楼梯很暗")]
    assert split_history_for_chapters(lines) == [(None, ["阿福：我下楼", "守秘人：楼梯很暗"])]


def test_each_split_group_gets_its_own_chapter_input() -> None:
    """分头那几组各摘一段，且每组的输入含公开行——只喂私密行会摘出没头没尾的怪话。"""
    lines = [HistoryLine("守秘人：你们在门厅分开了")] + [
        HistoryLine(f"地下室第 {i} 行", _CELLAR) for i in range(MIN_LINES_PER_CHAPTER)
    ]
    groups = split_history_for_chapters(lines)
    assert [audience for audience, _ in groups] == [None, _CELLAR]
    assert groups[0][1] == ["守秘人：你们在门厅分开了"]
    assert groups[1][1][0] == "守秘人：你们在门厅分开了"
    assert len(groups[1][1]) == 1 + MIN_LINES_PER_CHAPTER


def test_a_group_with_too_little_to_say_does_not_burn_a_model_call() -> None:
    """受众集合按"谁在场"算，隐匿/单人分头会造出很多只有一两行的小集合。

    那几行不会消失——它们仍在 L3 窗口里，只是不进长期摘要。
    """
    lines = [HistoryLine("守秘人：开场")] + [
        HistoryLine(f"只有他看见的第 {i} 行", _CELLAR) for i in range(MIN_LINES_PER_CHAPTER - 1)
    ]
    assert [audience for audience, _ in split_history_for_chapters(lines)] == [None]


# ── 受众覆盖全场 = 本来就是公开的（2026-08-15 实测）───────────
#
# 🔴 原来的退化保证写着「未分头时返回的就是一条公开段」，**在真实单人局里
# 不成立**：潜行/私密投递会给 narration 打上 audience，而单人局那个 audience
# 就是全部在场玩家。08-14 那 28 轮每次摘要都成对出现（间隔 2–3 秒、内容近似
# 但不完全相同）——2× LLM 调用，L2 记忆里还堆重复内容。


def test_an_audience_covering_everyone_is_treated_as_public() -> None:
    solo = frozenset({"p1"})
    lines = [
        HistoryLine("守秘人：门开了", audience=None),
        HistoryLine("阿福：我进去", audience=solo),
        HistoryLine("守秘人：里面很黑", audience=solo),
        HistoryLine("阿福：点灯", audience=solo),
    ]
    groups = split_history_for_chapters(lines, everyone=solo)

    assert len(groups) == 1, "单人局不该摘出两段给同一个人看"
    audience, texts = groups[0]
    assert audience is None
    assert len(texts) == 4, "并进公开段的行一条都不能丢"


def test_a_real_split_still_gets_its_own_chapter() -> None:
    """🔴 对照组：真的分头时仍然各摘一段。

    没有这一条，把整个分支改成"一律只出公开段"也会绿——那会让分头期间的
    剧情整段丢掉，正是 2026-08-11 补这个功能要解决的问题。
    """
    everyone = frozenset({"p-hall", "p-cellar"})
    lines = [
        HistoryLine("守秘人：你们分头了", audience=None),
        HistoryLine("阿福：我下地窖", audience=_CELLAR),
        HistoryLine("守秘人：地窖很潮", audience=_CELLAR),
        HistoryLine("阿福：我点灯", audience=_CELLAR),
    ]
    groups = split_history_for_chapters(lines, everyone=everyone)

    assert len(groups) == 2
    assert {a for a, _ in groups} == {None, _CELLAR}


def test_without_a_roster_the_old_behaviour_is_kept() -> None:
    """拿不到名单就不猜（显式降级）：行为与传 `everyone` 之前一致。"""
    solo = frozenset({"p1"})
    lines = [
        HistoryLine("守秘人：门开了", audience=None),
        HistoryLine("阿福：我进去", audience=solo),
        HistoryLine("守秘人：里面很黑", audience=solo),
        HistoryLine("阿福：点灯", audience=solo),
    ]
    assert len(split_history_for_chapters(lines)) == 2


# ── 🔴 调用点必须跟场景切换解绑（2026-08-16）──────────────────


@pytest.mark.asyncio
async def test_the_ceiling_path_really_produces_a_summary(room) -> None:
    """兜底那条路要真的走得通：没换场景、但攒够了拍数 → 摘一段出来。

    只测 `should_summarize` 不够——那是纯函数，它返回 True 不代表
    `_summarize_chapter` 会把 `scene_changed=False` 传给它（改之前那里写死的
    就是 True）。
    """
    factory, (room_id, player_id) = room
    await _add_actions(factory, room_id, player_id, CHAPTER_HARD_CEILING)
    client = _CountingClient()

    await _agent_with(factory, client)._summarize_chapter(
        room_id, [HistoryLine("阿福：我再翻一遍这个抽屉")], scene_changed=False
    )

    assert client.calls == 1
    async with factory() as db:
        assert len(await load_chapters(db, room_id=room_id)) == 1


def test_the_summary_call_site_is_not_nested_under_scene_changed() -> None:
    """🔴 摘要的调用点不许挂在「场景切换」那个条件里。

    挂在里面的话，`should_summarize` 新加的兜底上限**永远走不到**——不换场景
    就一次都不摘，那段剧情滚出 L3 之后再也重建不了。而这种失效**没有任何
    东西会变红**：`should_summarize` 的单测照常绿，因为它压根没被调到。

    这就是「两件事共用一个开关」那条判据（`keeper.phase` 已经栽过一次）：
    过渡拍的注入确实只该在换场景时做，摘要有它自己的判据，两者必须分开。
    """
    import ast
    from pathlib import Path as _Path

    source = (
        _Path(__file__).parent.parent / "app" / "core" / "keeper" / "runtime" / "agent.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # 建一张 子节点 → 父节点 的表，才能从调用点往上走
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    call_sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_spawn_chapter_summary"
    ]
    assert call_sites, "找不到 _spawn_chapter_summary 的调用点——测试自己过期了"

    for call in call_sites:
        node: ast.AST | None = call
        while node is not None:
            parent = parents.get(node)
            # 只看「这个节点是不是某个 if 的 body」，条件本身里的引用不算
            if isinstance(parent, ast.If) and any(node is stmt for stmt in parent.body):
                names = {n.id for n in ast.walk(parent.test) if isinstance(n, ast.Name)}
                assert "scene_changed" not in names, (
                    "摘要的调用点又被挂回「场景切换」条件里了——"
                    "兜底上限会永远走不到，而且不会有任何东西变红。"
                )
            node = parent
