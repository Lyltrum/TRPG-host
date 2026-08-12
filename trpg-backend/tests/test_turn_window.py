"""回合收集窗口（exec/14 P5.1）。

真人守秘人**同时听四个人说话、然后回应一次**，他从不说"你等他说完再说"。
原设计在裁决之前就把其他人拒掉了（`ACTION_IN_PROGRESS`），而
**汇总必须发生在裁决之前**。

最重要的一条是退化证明：**单人局窗口为 0、合并文本 == 原话**，行为与本功能
上线前逐字一致。
"""

from __future__ import annotations

from app.service.turn_window import (
    Submission,
    TurnWindowManager,
    merge_utterances,
)


def _sub(nickname: str, utterance: str) -> Submission:
    return Submission(player_id=f"p-{nickname}", nickname=nickname, utterance=utterance)


# ── 🔴 退化证明：单人局与上线前逐字一致 ──────────────────────


def test_solo_window_is_zero() -> None:
    """单人局不多等一毫秒。否则每轮凭空加几秒延迟，是纯粹的倒退。"""
    assert TurnWindowManager().window_seconds(1) == 0.0
    assert TurnWindowManager().window_seconds(0) == 0.0


def test_solo_merged_text_is_the_raw_utterance() -> None:
    """单条时不加昵称前缀——单人局的 prompt 因此与上线前逐字一致。"""
    assert merge_utterances([_sub("阿福", "我搜查书桌")]) == "我搜查书桌"


def test_multiplayer_opens_a_window() -> None:
    assert TurnWindowManager().window_seconds(2) > 0


# ── 合并 ─────────────────────────────────────────────────────


def test_multiple_submissions_are_merged_with_speaker_names() -> None:
    """多人时裁决器必须能看出**每个人分别**说了什么。"""
    merged = merge_utterances([_sub("阿福", "我搜查书桌"), _sub("小林", "我盯着门口")])
    assert merged == "阿福：我搜查书桌\n小林：我盯着门口"


# ── 窗口生命周期 ─────────────────────────────────────────────


def test_first_submission_opens_the_round_others_join_it() -> None:
    m = TurnWindowManager()
    assert m.join("room-1", _sub("阿福", "我搜查书桌")) is True
    assert m.join("room-1", _sub("小林", "我盯着门口")) is False
    assert m.join("room-1", _sub("老王", "我掏枪")) is False
    assert [s.nickname for s in m.drain("room-1")] == ["阿福", "小林", "老王"]


def test_drain_closes_the_window_so_the_next_submission_opens_a_new_round() -> None:
    m = TurnWindowManager()
    m.join("room-1", _sub("阿福", "第一轮"))
    m.drain("room-1")
    assert m.is_collecting("room-1") is False
    assert m.join("room-1", _sub("小林", "第二轮")) is True


def test_drain_is_idempotent() -> None:
    """🔴 `finally` 里会无条件再 drain 一次兜底——它必须是空操作而不是报错。

    窗口缓冲若在异常路径上漏掉，房间会永久停留在"收集中"：之后每条提交都
    只广播原话、静默不裁决，比锁死更难查（锁至少 60 秒后会自己过期）。
    """
    m = TurnWindowManager()
    m.join("room-1", _sub("阿福", "我搜查书桌"))
    assert len(m.drain("room-1")) == 1
    assert m.drain("room-1") == []
    assert m.drain("从来没有过的房间") == []


def test_rooms_do_not_share_buffers() -> None:
    m = TurnWindowManager()
    assert m.join("room-1", _sub("阿福", "A")) is True
    # 另一个房间的第一条同样应该开窗，不该被 room-1 的缓冲影响
    assert m.join("room-2", _sub("小林", "B")) is True
    assert [s.utterance for s in m.drain("room-1")] == ["A"]
    assert [s.utterance for s in m.drain("room-2")] == ["B"]


# ── 「还有人在敲字」：窗口的关闭条件（真人实测 2026-08-11）─────────


def test_typing_keeps_the_window_open_but_stops_on_its_own() -> None:
    """🔴 2.5 秒是给"同时按下发送"用的，不是给打字用的。

    真人第二个人才刚开始敲键盘，窗口早关了，他那条于是变成紧接着的另一轮——
    世界在两个人之间推进了一次，多人局退化回"一人一轮"。
    """
    m = TurnWindowManager()
    m.join("room-1", _sub("阿福", "我推门进去"))
    m.mark_typing("room-1", "p-阿贵", now=100.0)

    assert m.someone_still_typing("room-1", now=100.5) is True
    # 🔴 不依赖 typing:false —— 刷新/断网/关标签页都发不出它。过了 TTL 就当他停了
    assert m.someone_still_typing("room-1", now=100.0 + m.TYPING_TTL_SECONDS + 0.01) is False


def test_someone_who_already_spoke_cannot_hold_the_window_open() -> None:
    """已经提交过的人在敲的多半是下一轮要说的话——让他撑着就是另一种"一人一轮"。"""
    m = TurnWindowManager()
    m.join("room-1", _sub("阿福", "我推门进去"))
    m.mark_typing("room-1", "p-阿福", now=100.0)
    assert m.someone_still_typing("room-1", now=100.1) is False


def test_typing_state_does_not_leak_across_rounds_or_rooms() -> None:
    m = TurnWindowManager()
    m.join("room-1", _sub("阿福", "A"))
    m.mark_typing("room-1", "p-阿贵", now=100.0)
    m.mark_typing("room-2", "p-小林", now=100.0)
    # drain 关窗时连带清掉——否则上一轮残留的击键会把下一轮的窗口凭空撑开
    m.drain("room-1")
    assert m.someone_still_typing("room-1", now=100.1) is False
    assert m.someone_still_typing("room-2", now=100.1) is True


def test_clear_typing_is_idempotent_and_safe_on_unknown_rooms() -> None:
    m = TurnWindowManager()
    m.clear_typing("从来没有过的房间", "p-谁")
    m.mark_typing("room-1", "p-阿贵", now=100.0)
    m.clear_typing("room-1", "p-阿贵")
    m.clear_typing("room-1", "p-阿贵")
    assert m.someone_still_typing("room-1", now=100.1) is False


def test_the_cap_is_longer_than_the_base_window() -> None:
    """封顶必须真的比下限长，否则"还有人在敲就延长"整条是死代码。"""
    m = TurnWindowManager()
    assert m.WINDOW_MAX_SECONDS > m.WINDOW_SECONDS


# ── 接线：`_await_window` 真的会因为"有人在敲"而多等 ───────────


async def test_await_window_extends_while_someone_is_typing(monkeypatch) -> None:
    """🔴 上面那些只证明了"判断对不对"，这条证明**编排层真的用了它**。

    判断写对了但没人调用，是这个项目反复踩过的形状（「加了字段没有消费方」）。
    常量缩小到毫秒级跑，免得一条用例等 8 秒。
    """
    import asyncio

    from app.controller.ws import _await_window
    from app.service.turn_window import TurnWindowManager, turn_window_manager

    monkeypatch.setattr(TurnWindowManager, "WINDOW_SECONDS", 0.10)
    monkeypatch.setattr(TurnWindowManager, "WINDOW_MAX_SECONDS", 0.60)
    monkeypatch.setattr(TurnWindowManager, "TYPING_TTL_SECONDS", 0.20)
    monkeypatch.setattr("app.controller.ws._WINDOW_POLL_SECONDS", 0.01)

    room = "room-await"
    turn_window_manager.drain(room)  # 保证干净
    turn_window_manager.join(room, _sub("阿福", "我推门进去"))
    loop = asyncio.get_running_loop()

    # 没人在敲：到点就收（≈ WINDOW_SECONDS，绝不到封顶）
    started = loop.time()
    await _await_window(room, connected_players=2)
    quiet = loop.time() - started
    assert quiet < 0.30, f"没人在敲却等了 {quiet:.2f}s"

    # 有人一直在敲：一路延到封顶
    async def keep_typing() -> None:
        while True:
            turn_window_manager.mark_typing(room, "p-阿贵", now=loop.time())
            await asyncio.sleep(0.05)

    typer = asyncio.create_task(keep_typing())
    started = loop.time()
    await _await_window(room, connected_players=2)
    waited = loop.time() - started
    typer.cancel()
    turn_window_manager.drain(room)

    assert waited > 0.30, f"有人在敲却只等了 {waited:.2f}s"
    assert waited < 1.20, f"封顶没起作用，等了 {waited:.2f}s"
