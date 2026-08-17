"""理智检定卡片上必须有「当前理智」这个数字。

## 🔴 为什么值得一条测试

这个字段是「整条链都在，就是没人能用到」的教科书实例：`SanCheckRequestPayload`
有 `current_san` 字段、推送真的发出去、SDK 生成了类型、前端在读它——**只有值
从来没被填过**（`ws.py` 里硬写着 `current_san=None`）。

四层都完整，所以四层都不会变红。2026-08-17 真机跑到 SAN 检定那一拍才发现：
卡片弹出来了，理智值是空的。

第二条用例钉的不是行为而是**签名**：`current_san` 必须是必需参数。给它一个
默认值，漏传就重新变成静默的——那正是这个 bug 当初能活下来的原因。
"""

from __future__ import annotations

import inspect

import pytest

from app.controller.ws import _check_request_envelope, _current_san_of
from app.core.narration.contract import CheckRequestNotice
from app.dto.ws import SanCheckRequestPayload
from app.models.room import Character, Player, Room


def _san_notice(player_id: str = "p-1") -> CheckRequestNotice:
    return CheckRequestNotice(
        check_request_id="chk-1",
        kind="san",
        player_id=player_id,
        player_nickname="凌铭辉",
        skill=None,
        reason="目睹尸体",
    )


def test_the_san_card_carries_the_current_sanity() -> None:
    payload, event_type = _check_request_envelope(_san_notice(), current_san=84)

    assert event_type == "san.check.request"
    # 收窄联合类型：kind="san" 走的必然是这一支，断言它本身也是一条守护
    assert isinstance(payload, SanCheckRequestPayload)
    assert payload.current_san == 84, "卡片上没有当前理智值——玩家看到的是空白"
    # 序列化之后也要在（前端读的是这一份，不是 python 对象）
    assert payload.model_dump(by_alias=True)["currentSan"] == 84


def test_current_san_is_a_required_argument() -> None:
    """🔴 不许给它默认值。

    默认值会让"忘了传"退化成"静默发 None"——这个 bug 当初就是这么活下来的。
    必需参数漏传会当场炸，那才是我们要的。
    """
    param = inspect.signature(_check_request_envelope).parameters["current_san"]
    assert param.default is inspect.Parameter.empty, (
        "current_san 有了默认值：漏传会重新变成静默的 None，"
        "而那正是 2026-08-17 真机才发现的那个 bug"
    )
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, "应当是关键字参数，避免位置传错"


def test_a_skill_card_is_unaffected() -> None:
    """技能检定卡片不带理智值，也不该因为这个参数而改形状。"""
    notice = CheckRequestNotice(
        check_request_id="chk-2",
        kind="skill",
        player_id="p-1",
        player_nickname="凌铭辉",
        skill="skill-spot-hidden",
        target=55,
    )
    payload, event_type = _check_request_envelope(notice, current_san=84)
    assert event_type == "check.request"
    assert not hasattr(payload, "current_san")


@pytest.mark.asyncio
async def test_the_number_is_the_value_after_losses_not_the_maximum(db_session) -> None:
    """读的是**掉过之后的当前值**，不是建卡时的上限。

    建卡把上限写进 `derived_stats["SAN"]`，keeper 扣理智时会把原值备份成
    `SAN_MAX` 再改 `SAN`。拿错这一个键，玩家在掉了 20 点之后仍然看到满值——
    比显示空白更糟，因为它看起来是对的。
    """
    room = Room(room_code="SANCRD", room_name="理智卡片", max_players=2, phase="InGame")
    db_session.add(room)
    await db_session.flush()
    player = Player(room_id=room.id, nickname="凌铭辉", reconnect_token="rt-san")
    db_session.add(player)
    await db_session.flush()
    db_session.add(
        Character(
            room_id=room.id,
            player_id=player.id,
            status="complete",
            name="凌铭辉",
            background="",
            notes="",
            derived_stats={"SAN": 64, "SAN_MAX": 85},
        )
    )
    await db_session.commit()

    assert await _current_san_of(db_session, player.id) == 64


@pytest.mark.asyncio
async def test_a_missing_sheet_gives_none_rather_than_a_made_up_number(db_session) -> None:
    """查不到角色卡时是 None，**不伪造一个数字**。

    卡片上少一个数字是小事；显示一个编出来的理智值会让玩家按它做决定。
    """
    assert await _current_san_of(db_session, "no-such-player") is None
    assert await _current_san_of(None, "p-1") is None
