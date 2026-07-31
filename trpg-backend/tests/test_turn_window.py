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
