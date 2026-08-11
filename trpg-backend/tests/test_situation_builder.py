"""局面块组装器的受众裁剪（`situation.SituationBuilder`）。

## 为什么补这份用例

`exec/27` 阶段 4 抽出 `SituationBuilder` 之后做变异检验，把 `for_keeper()` 的
`audience=None`（全给）改成 `audience=frozenset()`（全裁掉），**742 条测试一条
都没红**。

那是个真缺口：`for_keeper()` 喂的是**裁决器**。它要是误裁了历史，裁决器就再也
看不到分头那半段剧情，裁决质量静默下滑——而叙事照常输出，读叙事永远发现不了。

磁带回放的 `drifts == []` 理论上能抓（请求指纹会变），实际抓不到：录制用的迷你
剧本里没有任何带受众的历史行，`visible_history(lines, frozenset())` 与
`visible_history(lines, None)` 结果相同。**覆盖率靠的是数据里有没有那种情况，
不是断言写没写。**

## 钉的是哪条规则

> `visibility="private"` 的私密是**玩家↔玩家**，**主持人永远看得见**
> ——KP 不知道就没法主持（`exec/18` 定稿）。
"""

from __future__ import annotations

from app.core.keeper.memory.chapter import Chapter
from app.core.keeper.memory.history import HistoryLine
from app.core.keeper.narration.situation import SituationBuilder

_PUBLIC = "门厅里积着灰"
_PRIVATE = "地下室的铁柜上刻着一个名字"
_PUBLIC_CHAPTER = "上半场大家在门厅碰头"
_PRIVATE_CHAPTER = "他一个人撬开了地下室的木箱"


def _builder() -> SituationBuilder:
    return SituationBuilder(
        visible_state={"当前场景": "门厅"},
        history_lines=[
            HistoryLine(f"守秘人：{_PUBLIC}。", None),
            HistoryLine(f"守秘人：{_PRIVATE}。", frozenset({"p1"})),
        ],
        roster=["张家豪", "凌铭辉"],
        phase="investigation",
        phase_status="调查阶段",
        ledger_status="（无）",
        chapters=[
            Chapter(_PUBLIC_CHAPTER),
            Chapter(_PRIVATE_CHAPTER, frozenset({"p1"})),
        ],
        capability_blocks=[],
        narrator_capability_blocks=[],
        is_heartbeat=False,
        is_opening_ceremony=False,
    )


def test_the_keeper_sees_every_line_including_the_private_ones() -> None:
    """🔴 主持人永远看得见——他不知道就没法主持。

    变异检验入口：把 `for_keeper` 的 `audience=None` 改成任何一个具体受众，
    这条会红。
    """
    text = _builder().for_keeper(nickname="张家豪", utterance="我翻抽屉")
    assert _PUBLIC in text
    assert _PRIVATE in text


def test_a_group_only_sees_what_it_was_there_for() -> None:
    """分组叙事：不在场的那组读不到那一行——**模型拿不到才是真的漏不出去**。"""
    builder = _builder()
    for_p1 = builder.render(
        audience=frozenset({"p1"}), ledger="（无）", nickname="张家豪", utterance="我翻抽屉"
    )
    for_p2 = builder.render(
        audience=frozenset({"p2"}), ledger="（无）", nickname="凌铭辉", utterance="我看窗外"
    )
    assert _PRIVATE in for_p1
    assert _PRIVATE not in for_p2
    # 公开行两组都看得见
    assert _PUBLIC in for_p1 and _PUBLIC in for_p2


def test_the_ledger_is_supplied_per_call_not_taken_from_the_field() -> None:
    """账本按受众算好后由调用方传进来，`render` 不许偷偷用守秘人那份。

    没有这一条，分头时另一半挣到的线索会从「已确认的线索」小节漏过去——
    历史裁了、账本没裁，等于白裁。
    """
    text = _builder().render(
        audience=frozenset({"p2"}),
        ledger="- 这一组自己的线索",
        nickname="凌铭辉",
        utterance="我看窗外",
    )
    assert "这一组自己的线索" in text
    assert "（无）" not in text.split("## 游戏历史")[0].split("## 已确认的线索")[-1]


def test_the_prologue_block_is_cut_by_audience_too() -> None:
    """🔴 前情提要曾经对所有受众是同一份字符串——历史裁了、摘要没裁等于白裁。

    而摘要比历史更该裁：它**常驻上下文**（不设 limit、活过 L3 的 200 条窗口），
    漏一次就一直漏。
    """
    builder = _builder()
    for_p1 = builder.render(
        audience=frozenset({"p1"}), ledger="（无）", nickname="张家豪", utterance="我翻抽屉"
    )
    for_p2 = builder.render(
        audience=frozenset({"p2"}), ledger="（无）", nickname="凌铭辉", utterance="我看窗外"
    )
    assert _PRIVATE_CHAPTER in for_p1
    assert _PRIVATE_CHAPTER not in for_p2
    assert _PUBLIC_CHAPTER in for_p1 and _PUBLIC_CHAPTER in for_p2
    # 守秘人照旧全给
    assert _PRIVATE_CHAPTER in builder.for_keeper(nickname="张家豪", utterance="我翻抽屉")


def test_an_empty_audience_sees_only_public_lines() -> None:
    """🔴 空受众 = 没有人，只给公开行。

    `frozenset() <= x` 恒为真，所以不显式挡住的话"谁都不是"会拿到**全部**历史
    （含私密行）——**朝泄密方向失败**，跟 `visible_history` docstring 声称的
    "朝保密方向失败"正相反。

    目前不可达（分组来自 `group_players`，恒非空），但可达性是调用方的性质，
    不是这个函数的保证。

    发现过程值得记：exec/27 阶段 4 的变异检验里，把 `for_keeper` 的
    `audience=None` 改成 `frozenset()` 后测试全绿。第一反应是"覆盖不够"，
    查下去才发现那是个**无效变异体**——两者行为本来就相同。**恰恰因为它不改
    行为，才暴露了这里。**
    """
    text = _builder().render(
        audience=frozenset(), ledger="（无）", nickname="无人", utterance="（无）"
    )
    assert _PUBLIC in text
    assert _PRIVATE not in text
    assert _PUBLIC_CHAPTER in text
    assert _PRIVATE_CHAPTER not in text
