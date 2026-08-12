"""局末复盘：代码统计那一半（下半那段回顾要打网络，这里不碰）。

🔴 **数字全部代码算**是这条线的主张，所以测试的重心也在数字：模型只写那段
散文，写错数字的机会从一开始就不给它。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

from app.core.keeper.primitives.dice import LEVEL_FAIL, LEVEL_FUMBLE, LEVEL_REGULAR
from app.models.event import Event
from app.models.room import Room
from app.service.recap import build_highlights
from tests.helpers import ROOMS_BASE, create_room, reconnect


def _room(**kwargs) -> Room:  # noqa: ANN003
    started = datetime(2026, 8, 12, 20, 0, tzinfo=UTC)
    defaults = {
        "room_code": "RCP001",
        "room_name": "复盘房",
        "max_players": 4,
        "phase": "Completed",
        "created_at": started,
        "ended_at": started + timedelta(hours=3, minutes=12),
    }
    return Room(**{**defaults, **kwargs})


def _event(event_type: str, payload: dict | None = None) -> Event:
    return Event(room_id="r", event_type=event_type, payload=payload or {})


def test_a_quiet_room_gets_no_noise_lines() -> None:
    """一次检定都没掷的局不该出现"掷骰 0 次"——那是噪音（同局面块的退化保证）。"""
    lines = build_highlights(_room(ended_at=None, created_at=None), [])
    assert lines == []


def test_duration_reads_like_a_person_would_say_it() -> None:
    lines = build_highlights(_room(), [])
    assert lines == ["这一局跑了 3 小时 12 分钟"]


def test_checks_are_counted_by_level_not_by_negation() -> None:
    """🔴 **大失败不是"失败"这个字符串**。用 `!= 失败` 判成功会把大失败算成成功
    ——这条用例就是钉那个否定式的。"""
    events = [
        _event("keeper.check", {"level": LEVEL_REGULAR}),
        _event("keeper.check", {"level": LEVEL_FAIL}),
        _event("keeper.check", {"level": LEVEL_FUMBLE}),
    ]

    line = next(li for li in build_highlights(_room(), events) if "检定" in li)

    assert "掷了 3 次检定，成功 1 次" in line
    assert "大失败 1 次" in line


def test_san_loss_is_summed_from_the_events(client: AsyncClient) -> None:  # noqa: ARG001
    events = [
        _event("keeper.san", {"loss": 3}),
        _event("keeper.san", {"loss": 6}),
    ]

    line = next(li for li in build_highlights(_room(), events) if "SAN" in li)

    assert "理智检定 2 次" in line and "掉了 9 点" in line


def test_a_madness_bout_names_each_person_once() -> None:
    events = [
        _event("keeper.madness", {"player": "阿福"}),
        _event("keeper.madness", {"player": "阿福"}),
        _event("keeper.madness", {"player": "阿贵"}),
    ]

    line = next(li for li in build_highlights(_room(), events) if "疯狂" in li)

    assert line.count("阿福") == 1 and "阿贵" in line


def test_damage_is_reported_only_when_they_actually_got_hurt() -> None:
    """治疗（正的 delta）不该变成"挨了 -3 点伤"。"""
    healed_only = build_highlights(_room(), [_event("keeper.hp", {"delta": 3})])
    assert not any("挨了" in li for li in healed_only)

    hurt = build_highlights(
        _room(), [_event("keeper.hp", {"delta": -4}), _event("keeper.hp", {"delta": 2})]
    )
    assert any("挨了 2 点伤" in li for li in hurt)


def test_a_malformed_payload_does_not_crash_the_recap() -> None:
    """事件流是历史数据，形状不保证。复盘打不开比数字少一行糟得多。"""
    events = [
        _event("keeper.check", {}),
        _event("keeper.san", {"loss": None}),
        _event("keeper.hp", {}),
    ]

    lines = build_highlights(_room(), events)

    assert any("掷了 1 次检定，成功 0 次" in li for li in lines)
    assert any("掉了 0 点 SAN" in li for li in lines)


# ── 走 HTTP ──────────────────────────────────────


async def test_summary_is_no_longer_not_implemented(client: AsyncClient) -> None:
    """🔴 **行为变更**：这个端点此前恒抛 NOT_IMPLEMENTED（`room_summaries`
    表建了但没有任何写入路径）。"""
    room = await create_room(client)

    response = await client.get(f"{ROOMS_BASE}/{room['roomId']}/summary")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["roomId"] == room["roomId"]
    assert isinstance(data["highlights"], list)


async def test_without_an_api_key_there_is_no_fabricated_recap(client: AsyncClient) -> None:
    """没有 key 就只有数字那一半。**编一段假的比没有更糟**——测试环境不配 key，
    所以这条同时也是"降级是显式的"的守卫。"""
    room = await create_room(client)

    data = (await client.get(f"{ROOMS_BASE}/{room['roomId']}/summary")).json()["data"]

    assert data["summaryText"] is None


async def test_a_finished_room_caches_its_recap(client: AsyncClient) -> None:
    """结束之后算一次就落库：复盘会被反复打开，事件流却不再变了。"""
    room = await create_room(client)
    module_id = (await client.get("/api/v1/modules")).json()["data"][0]["id"]
    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/module",
        json={"moduleId": module_id, "attributeGenMethod": "point_buy"},
        headers=reconnect(room["reconnectToken"]),
    )
    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/disband",
        json=None,
        headers=reconnect(room["reconnectToken"]),
    )

    first = (await client.get(f"{ROOMS_BASE}/{room['roomId']}/summary")).json()["data"]
    second = (await client.get(f"{ROOMS_BASE}/{room['roomId']}/summary")).json()["data"]

    assert first == second
    assert any("这一局跑了" in li for li in first["highlights"])


async def test_an_unknown_room_is_a_404(client: AsyncClient) -> None:
    response = await client.get(f"{ROOMS_BASE}/00000000-0000-0000-0000-0000000000ff/summary")
    assert response.status_code == 404
