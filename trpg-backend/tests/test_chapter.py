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
    CHAPTER_EVENT_CEILING,
    CHAPTER_MAX_CHARS,
    MIN_LINES_PER_CHAPTER,
    MIN_TURNS_BETWEEN_CHAPTERS,
    Chapter,
    build_recap,
    events_since_last_chapter,
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
    assert should_summarize(scene_changed=True, turns_since_last=1, events_since_last=0) is False


def test_turns_alone_is_not_enough_below_the_ceiling() -> None:
    """只按轮数会把一段完整的戏拦腰截断。

    🔴 2026-08-16 收窄：原来断言的是 `turns=999` 也不摘，而那正是缺陷——
    不换场景就永远不摘。现在的规则是「**在兜底上限以内**，光有轮数不够」。
    """
    assert (
        should_summarize(
            scene_changed=False,
            turns_since_last=MIN_TURNS_BETWEEN_CHAPTERS * 3,
            events_since_last=CHAPTER_EVENT_CEILING - 1,
        )
        is False
    )


def test_both_conditions_trigger() -> None:
    assert (
        should_summarize(
            scene_changed=True,
            turns_since_last=MIN_TURNS_BETWEEN_CHAPTERS,
            events_since_last=0,
        )
        is True
    )


def test_the_ceiling_fires_even_without_a_scene_change() -> None:
    """🔴 兜底：待在同一个场景里太久也必须摘一次。

    没有这条时，地下室搜半小时、审一个 NPC 审二十轮都不会产生任何摘要，
    而那段剧情滚出 L3 之后**再也没人能重建它**——L1 能从 id 重渲、L3 原文
    还在库里，只有 L2 是一次性的。
    """
    assert (
        should_summarize(
            scene_changed=False, turns_since_last=0, events_since_last=CHAPTER_EVENT_CEILING
        )
        is True
    )


def test_the_ceiling_is_derived_from_the_history_window() -> None:
    """🔴 兜底上限必须**显著小于 L3 窗口**，否则它兜不住底。

    要跟着 `HISTORY_LIMIT` 走：写死一个数的话，哪天有人调大/调小历史窗口，
    安全边际会静默失效——失效的表现是"某几段剧情没了"，不会有任何东西变红。

    ## 🔴 这条测试上一版是瞎的（2026-08-19 修）

    它原来写的是：

        beats_covered = HISTORY_LIMIT / 2.5
        assert beats_covered / 2 > CHAPTER_HARD_CEILING

    `2.5` 正是**被测代码自己用的那个折算系数**——测试拿被测对象的错误假设当
    自己的依据，于是无论那个系数错得多离谱，两边都会一致地错下去，测试永远绿。
    同 `exec/43` 那条「守护测试拿被测的那张表自己当样本来源」。

    现在两边的单位都是**事件条数**，中间不再有任何折算，这个洞就消失了。
    """
    assert CHAPTER_EVENT_CEILING < HISTORY_LIMIT, "兜底比窗口还大 ⇒ 结构上不可能兜住底"
    assert HISTORY_LIMIT / CHAPTER_EVENT_CEILING >= 2, "余量不足两倍，密度一波动就来不及"


def test_the_ceiling_beats_the_window_on_a_real_long_game() -> None:
    """🔴 **实锤复现**（`LG4LWD`，112 拍单人真机局）。

    上一版的兜底是 53 **拍**，而那局**第 53 拍时已累计 414 条事件**——超过
    `HISTORY_LIMIT`（400）。也就是说兜底还没触发，这段剧情的开头就已经滚出
    L3 了，而摘要模型读的正是 L3 窗口（`agent._summarize_chapter` 收的是这一轮
    加载的 `history_lines`）⇒ 它摘不到要摘的东西。

    这里把那局的真实密度钉成一个数：**7.8 条事件/拍**（414/53）。按新判据，
    兜底在第 `CHAPTER_EVENT_CEILING` 条事件时就该触发，那时窗口才用掉一半。

    **变异检验**：把 `CHAPTER_EVENT_CEILING` 改回 `int(HISTORY_LIMIT / 2.5 / 3)`
    并按拍计数，这条当场红。
    """
    events_per_beat = 414 / 53  # LG4LWD 实测
    beats_when_ceiling_fires = CHAPTER_EVENT_CEILING / events_per_beat

    # 触发时窗口还没满 —— 这正是上一版做不到的那件事
    assert CHAPTER_EVENT_CEILING < HISTORY_LIMIT
    # 而且要早得多：留下的余量够再跑一整段同样长的戏
    assert beats_when_ceiling_fires < 53, "兜底触发得比出问题的那一版还晚，等于没修"


def test_the_ceiling_is_immune_to_party_size() -> None:
    """🔴 判据不许随人数漂移——这是换单位的**根本理由**。

    实测 `action.submit / keeper.decision` 随人数单调上升（1 人 0.82 / 2 人 1.04
    / 3 人 1.75 / 4 人 2.50：几个人的发言被收进同一拍）。旧判据数的是
    `action.submit`，于是同一个 53 在单人局是 53 个真实回合、在四人局只有 21 个
    ——**同一条规则在多人局自动变成另一条规则**（同 `exec/45` SAN 窗口那条）。

    新判据数的是全部事件，而 L3 装的也是全部事件，两者**同一个单位**，人数
    再怎么变都不进入这个判断。这条用四种人数的实测密度各跑一遍，断言触发点
    落在同一个事件数上。
    """
    for submit_per_beat in (0.82, 1.04, 1.75, 2.50):
        # 不管一拍里有几条玩家发言，兜底问的都是"窗口用掉多少了"
        assert (
            should_summarize(
                scene_changed=False,
                turns_since_last=int(CHAPTER_EVENT_CEILING / submit_per_beat),
                events_since_last=CHAPTER_EVENT_CEILING,
            )
            is True
        )
        assert (
            should_summarize(
                scene_changed=False,
                turns_since_last=int(CHAPTER_EVENT_CEILING / submit_per_beat),
                events_since_last=CHAPTER_EVENT_CEILING - 1,
            )
            is False
        )


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
    """兜底那条路要真的走得通：没换场景、但攒够了**事件** → 摘一段出来。

    只测 `should_summarize` 不够——那是纯函数，它返回 True 不代表
    `_summarize_chapter` 会把 `scene_changed=False` 传给它（改之前那里写死的
    就是 True），也不代表调用点真的去查了事件数（2026-08-19 换单位时，
    漏接 `events_since_last_chapter` 会让它永远收到 0）。
    """
    factory, (room_id, player_id) = room
    await _add_actions(factory, room_id, player_id, CHAPTER_EVENT_CEILING)
    client = _CountingClient()

    await _agent_with(factory, client)._summarize_chapter(
        room_id, [HistoryLine("阿福：我再翻一遍这个抽屉")], scene_changed=False
    )

    assert client.calls == 1
    async with factory() as db:
        assert len(await load_chapters(db, room_id=room_id)) == 1


@pytest.mark.asyncio
async def test_the_ceiling_counts_events_not_player_turns(room) -> None:
    """🔴 **守的是「单位」本身**，而不是那个数值。

    上一版按 `action.submit` 条数算，这一版按全部事件条数算。区分这两者需要一个
    **两种单位下结论相反**的样本——现有的兜底集成测试造的全是 `action.submit`，
    两种算法都会触发，它区分不开（造的样本没走到被测分支 = 没测）。

    这里造的是：**少量玩家发言 + 大量叙事/裁决事件**。真实长局就长这样——
    `LG4LWD` 实测 112 拍产生 844 条事件，`narration.push` 和 `keeper.decision`
    才是大头。

    - 按事件数：`CHAPTER_EVENT_CEILING` 条 ⇒ **该摘**
    - 按拍数：只有 3 拍，远不到任何阈值 ⇒ 不摘

    **变异检验**：把 `agent._summarize_chapter` 里的 `events_since_last=events`
    改成 `events_since_last=turns`（= 退回按拍算），这条当场红。
    """
    factory, (room_id, player_id) = room
    await _add_actions(factory, room_id, player_id, 3)
    async with factory() as db:
        for i in range(CHAPTER_EVENT_CEILING):
            db.add(
                Event(
                    room_id=room_id,
                    player_id=None,
                    event_type="narration.push",
                    payload={"text": f"第 {i} 段叙事"},
                )
            )
        await db.commit()

    client = _CountingClient()
    await _agent_with(factory, client)._summarize_chapter(
        room_id, [HistoryLine("阿福：我再翻一遍这个抽屉")], scene_changed=False
    )

    assert client.calls == 1, "大量非发言事件撑满了窗口，兜底必须触发"
    async with factory() as db:
        assert len(await load_chapters(db, room_id=room_id)) == 1


@pytest.mark.asyncio
async def test_a_quiet_stretch_of_pure_turns_does_not_trigger_the_ceiling(room) -> None:
    """对照组：光有拍数、事件没攒够，兜底不该触发。

    没有这条的话，上面那条可以被"永远返回 True"的退化实现骗过去。
    """
    factory, (room_id, player_id) = room
    await _add_actions(factory, room_id, player_id, MIN_TURNS_BETWEEN_CHAPTERS * 2)

    client = _CountingClient()
    await _agent_with(factory, client)._summarize_chapter(
        room_id, [HistoryLine("阿福：我再翻一遍这个抽屉")], scene_changed=False
    )

    assert client.calls == 0


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


# ── 兜底数的集合，必须与 L3 装的集合相同（2026-08-23）──────────


async def test_the_ceiling_counts_only_what_the_window_actually_holds(room) -> None:
    """🔴 **同一个错误的第三张脸。**

    | 时间 | 判据 | 错在哪 |
    |---|---|---|
    | 最初 | 距上次 53 **拍** | 折算系数 2.5 错了 |
    | 2026-08-19 | 距上次 200 **条事件** | 单位对了，数的却是**全部事件** |
    | 2026-08-23 | 距上次 200 条**进得了 L3 的事件** | 集合终于跟窗口对上 |

    L3 装的不是全部事件——`agent.py` 按 `HISTORY_EVENT_TYPES` 过滤，实测一场
    104 拍的真机局里 759 条事件只有 265 条（35%）进得了窗口。而兜底问的是
    「窗口用掉多少了」⇒ 数别的集合就是在答别的问题。

    **变异检验**：把 `events_since_last_chapter` 里那个 `in_(HISTORY_EVENT_TYPES)`
    去掉，这条当场红（计数从 0 变成 `CHAPTER_EVENT_CEILING`）。
    """
    factory, (room_id, player_id) = room
    async with factory() as db:
        for _ in range(CHAPTER_EVENT_CEILING):
            db.add(Event(room_id=room_id, event_type="keeper.decision", payload={}))
        await db.commit()
    async with factory() as db:
        counted = await events_since_last_chapter(db, room_id=room_id)
    assert counted == 0, (
        f"数出来 {counted} 条，而这些事件一条都进不了 L3 —— 兜底会在窗口空着的时候触发"
    )
    assert not should_summarize(scene_changed=False, turns_since_last=0, events_since_last=counted)


async def test_the_ceiling_still_fires_on_events_the_window_does_hold(room) -> None:
    """🔴 上一条的对侧：**别把兜底一起关掉了。**

    只验"不该触发时不触发"的话，`return 0` 这个退化实现照样全绿
    （同 `judgement-as-conversation` 那条：两头都要验）。
    """
    factory, (room_id, player_id) = room
    await _add_actions(factory, room_id, player_id, CHAPTER_EVENT_CEILING)
    async with factory() as db:
        counted = await events_since_last_chapter(db, room_id=room_id)
    assert counted == CHAPTER_EVENT_CEILING
    assert should_summarize(scene_changed=False, turns_since_last=0, events_since_last=counted)


def test_nobody_re_lists_the_history_event_types() -> None:
    """「一份知识写两遍」的守门人。

    L3 装哪些类型只有一处定义（`HISTORY_EVENT_TYPES`）。摘要这边要是自己再列
    一份，两份清单会慢慢分叉，**而两头都不会变红**——上面那个 bug 正是这么活
    下来的（注释信誓旦旦写着"L3 装的就是全部事件"，而它不是）。
    """
    import ast
    from pathlib import Path as _Path

    source = (
        _Path(__file__).parent.parent / "app" / "core" / "keeper" / "memory" / "chapter.py"
    ).read_text(encoding="utf-8")
    assert "HISTORY_EVENT_TYPES" in source, "摘要根本没引用那份清单"
    tree = ast.parse(source)
    literals = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value in ("narration.push", "keeper.check")
    ]
    assert not literals, "摘要模块里出现了 L3 事件类型的字面量 —— 那是第二份清单"


async def test_only_the_new_stretch_gets_summarised(room, monkeypatch) -> None:
    """🔴 **摘的是「距上次摘要以来」，不是整个 L3 窗口。**

    喂全量的后果不是"模型不听话"——摘要 prompt 说的是「把下面这段游戏历史压缩
    成一句话」，而我们给它的就是"从头"，**它从头讲起是正确执行**。

    实测（2026-08-23，350 拍单人局）：11 段摘要里前 7 段全是同一个故事的不同
    长度版本，一个细节都没有；探针问已滚出 L3 的两件事，**两条都编了一个**
    （借书卡"第 47 页"实为 88、灯的外号"歪脖子"实为"瘸腿老头"），语气跟答对
    的对照组一样确信。

    **变异检验**：把 `history_lines[-events:]` 改回 `history_lines`，这条当场红。
    """
    from app.core.keeper.runtime.agent import KeeperAgent

    factory, (room_id, player_id) = room
    # 上一段：10 条，已经摘过；新的一段：14 条（要过 MIN_TURNS_BETWEEN_CHAPTERS）
    await _add_actions(factory, room_id, player_id, 10)
    async with factory() as db:
        await record_chapter(db, room_id=room_id, text="上一段的梗概", audience=None)
        await db.commit()
    await _add_actions(factory, room_id, player_id, 14)

    seen: list[list[str]] = []

    async def _fake_summarize(_client, lines):
        seen.append(list(lines))
        return "梗概"

    monkeypatch.setattr("app.core.keeper.runtime.agent.summarize_chapter", _fake_summarize)

    agent = KeeperAgent.__new__(KeeperAgent)
    agent._session_factory = factory
    agent._client = None
    history = [HistoryLine(text=f"第 {i} 行") for i in range(24)]
    await agent._summarize_chapter(room_id, history, frozenset(), scene_changed=True)

    assert seen, "装置自证：摘要根本没被触发，下面的断言就没有意义"
    fed = seen[0]
    assert len(fed) == 14, f"喂了 {len(fed)} 行 —— 应该只有新产生的那 14 行"
    assert fed[0] == "第 10 行", "从上一段摘要之后那一行开始，不是从窗口开头"
    assert fed[-1] == "第 23 行"
