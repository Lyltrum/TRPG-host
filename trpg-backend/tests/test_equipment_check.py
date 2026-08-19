"""建卡时的装备合理性校验（2026-08-16）。

判据与 prompt 在 `app/core/equipment_check.py`，接入点是 `complete_character`
——**唯一那道闸门**，向导提交和「用我的常用卡」走的都是它。

🔴 这里一次真实 API 都不打：`EquipmentChecker` 整个被换掉，测的是**接线**
（什么时候调、判不合理时拦不拦、判不成时放不放）和**保密边界**（prompt 里
有什么、没有什么）。模型判得准不准不是单测能回答的问题。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.core.equipment_check import EquipmentVerdict, RejectedItem, clamp_items, rejection_message
from tests.helpers import ROOMS_BASE, create_room, reconnect
from tests.test_characters import BUILT_CHARACTER

_COMPLETE = "{base}/{room}/characters/{cid}/complete"


class _StubChecker:
    """替身。记下收到的 prompt，返回预设结论。"""

    prompts: list[str] = []

    def __init__(self, verdict: EquipmentVerdict | None) -> None:
        self._verdict = verdict

    def __call__(self, _api_key: str) -> _StubChecker:
        return self

    async def check(self, prompt: str) -> EquipmentVerdict | None:
        type(self).prompts.append(prompt)
        return self._verdict


@pytest.fixture(autouse=True)
def _clear_prompts():
    _StubChecker.prompts = []
    yield
    _StubChecker.prompts = []


def _install(monkeypatch: pytest.MonkeyPatch, verdict: EquipmentVerdict | None) -> None:
    """把审核器换成替身，并让服务层认为"配了 key"。"""
    from app.core.config import get_settings
    from app.service import character as character_service

    patched = get_settings().model_copy(update={"deepseek_api_key": "test-key-not-a-real-one"})
    monkeypatch.setattr(character_service, "get_settings", lambda: patched)
    monkeypatch.setattr(character_service, "EquipmentChecker", _StubChecker(verdict))


async def _build(client: AsyncClient, equipment: list[dict] | None = None) -> tuple[dict, str]:
    """建到"差最后一步 complete"的状态，返回 (room, character_id)。"""
    room = await create_room(client)
    draft = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters", headers=reconnect(room["reconnectToken"])
    )
    character_id = draft.json()["data"]["characterId"]
    payload = dict(BUILT_CHARACTER)
    if equipment is not None:
        payload["equipment"] = equipment
    saved = await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        json=payload,
        headers=reconnect(room["reconnectToken"]),
    )
    assert saved.status_code == 200, saved.text
    return room, character_id


async def _complete(client: AsyncClient, room: dict, character_id: str):
    return await client.post(
        _COMPLETE.format(base=ROOMS_BASE, room=room["roomId"], cid=character_id),
        headers=reconnect(room["reconnectToken"]),
    )


# ── 拦截 ─────────────────────────────────────────


async def test_an_impossible_item_blocks_the_build(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 拦截是硬的（用户 2026-08-16 拍板）：判为拿不到就不许 complete。"""
    _install(
        monkeypatch,
        EquipmentVerdict(
            rejected=[
                RejectedItem(
                    item="手机", reason="1925 年还没有移动电话", alternatives=["怀表", "电报"]
                )
            ]
        ),
    )
    room, character_id = await _build(client, [{"name": "手机"}, {"name": "手电筒"}])

    response = await _complete(client, room, character_id)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["code"] == "CHARACTER_INVALID"
    issues = body["error"]["details"]
    assert [i["code"] for i in issues] == ["EQUIPMENT_IMPLAUSIBLE"]
    # 🔴 `field` 带上**是哪一件**（2026-08-19）：前端要就地给这件东西一个
    # 「说明来路」的输入框，只有一句拼好的话定位不到具体哪一项。沿用
    # `skills.spot-hidden` 的既有路径语义。
    assert issues[0]["field"] == "equipment.手机"
    # 说清楚为什么不行 **并且** 改成什么——只说"不行"玩家只能瞎猜
    message = issues[0]["message"]
    assert "手机" in message
    assert "1925" in message
    assert "怀表" in message


async def test_the_player_stays_unready_when_blocked(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 被拦住就是没建完：`hasCharacter` 不许变 True。

    只断言 400 是不够的——先落库再抛异常同样会返回 400，而那时这张卡已经算数了。
    """
    _install(monkeypatch, EquipmentVerdict(rejected=[RejectedItem(item="手机")]))
    room, character_id = await _build(client, [{"name": "手机"}])

    await _complete(client, room, character_id)

    preview = await client.get(f"{ROOMS_BASE}/{room['roomCode']}")
    host = next(p for p in preview.json()["data"]["players"] if p["isHost"])
    assert host["hasCharacter"] is False


# ── 放行 ─────────────────────────────────────────


async def test_a_plausible_list_passes(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, EquipmentVerdict(rejected=[]))
    room, character_id = await _build(client, [{"name": "手电筒"}, {"name": "笔记本"}])

    assert (await _complete(client, room, character_id)).status_code == 200


async def test_a_failed_judgement_lets_the_player_through(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 「模型说不行」和「模型没说话」是两回事。

    超时 / JSON 崩了 / 服务挂了都返回 None，那时必须放行——把可用性押给第三方
    服务不叫严格。这条是那个区分的守卫：改成 `if verdict is None: 拦` 会变红。
    """
    _install(monkeypatch, None)
    room, character_id = await _build(client, [{"name": "手机"}])

    assert (await _complete(client, room, character_id)).status_code == 200


async def test_no_api_key_means_no_call_at_all(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI / e2e 不配 key，那里根本不该发生这次调用。

    🔴 显式把 key 抹成 None，不靠"本机恰好没配"——开发机的 `.env` 里是有 key 的，
    靠环境去表达前提的测试在另一台机器上会悄悄测的是别的东西。
    """
    from app.core.config import get_settings
    from app.service import character as character_service

    without_key = get_settings().model_copy(update={"deepseek_api_key": None})
    monkeypatch.setattr(character_service, "get_settings", lambda: without_key)
    monkeypatch.setattr(character_service, "EquipmentChecker", _StubChecker(None))
    room, character_id = await _build(client, [{"name": "手机"}])

    assert (await _complete(client, room, character_id)).status_code == 200
    assert _StubChecker.prompts == []


async def test_an_empty_list_is_not_worth_a_round_trip(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没写装备就别花那 5 秒——空清单没有任何可判的东西。"""
    _install(monkeypatch, EquipmentVerdict(rejected=[]))
    room, character_id = await _build(client, [])

    assert (await _complete(client, room, character_id)).status_code == 200
    assert _StubChecker.prompts == []


# ── 保密边界与素材 ────────────────────────────────


async def test_the_prompt_carries_who_and_when_but_never_the_script(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """🔴 判断需要的三类素材都在，剧本正文一个字都不在。

    用户 2026-08-16 提的那两条（持枪按国家、物品按身份）就靠居住地与职业这两项
    落地——少一项，那两类判断在结构上就做不出来。
    """
    _install(monkeypatch, EquipmentVerdict(rejected=[]))
    room, character_id = await _build(client, [{"name": "左轮手枪"}])
    await _complete(client, room, character_id)

    assert len(_StubChecker.prompts) == 1
    prompt = _StubChecker.prompts[0]
    assert "私家侦探" in prompt, "职业缺席 ⇒ 「这个身份能不能有」判不了"
    assert "信用评级" in prompt, "买不买得起判不了"
    assert "居住地" in prompt, "「哪个国家能持枪」判不了"
    assert "左轮手枪" in prompt
    # 保密边界：这里只该有 era/tone 两个标量，不该有任何剧本内容
    assert "kp_truth" not in prompt
    assert "曾是警察" not in prompt, "背景故事不在参数表里"


# ── 纯函数 ───────────────────────────────────────


def test_blank_items_are_dropped_and_the_list_is_capped() -> None:
    assert clamp_items(["  手电筒 ", "", "   ", "绳子"]) == ["手电筒", "绳子"]
    assert len(clamp_items([f"物品{i}" for i in range(100)])) == 30


def test_the_message_says_why_and_what_instead() -> None:
    message = rejection_message(
        RejectedItem(item="手机", reason="1925 年还没有移动电话", alternatives=["怀表", "电报"])
    )
    assert "手机" in message
    assert "1925 年还没有移动电话" in message
    assert "怀表、电报" in message


def test_a_rejection_without_suggestions_still_reads_as_a_sentence() -> None:
    """模型偷懒不给替代品时，玩家至少要知道是哪一件被拦了。"""
    message = rejection_message(RejectedItem(item="等离子步枪"))
    assert "等离子步枪" in message
    assert message.strip()
