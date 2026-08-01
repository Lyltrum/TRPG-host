"""一键生成的角色卡背景（exec/23 #55 遗留的另一半）。

分两层验：
- `BackgroundWriter` 自己——成功、模型崩、超时各是什么结果；
- 接线——写不出来时卡照常成立，写出来时确实落到了 `background_detail`。

第二层是这组用例的重点。**背景是可选润色，建卡才是玩家在等的事**：一个
"LLM 挂了导致建不出卡"的回归，比背景写得难看严重一个量级。
"""

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.background_writer import (
    BackgroundWriter,
    CharacterBackground,
    build_prompt,
    to_detail,
)
from app.main import app
from app.models.room import Character, Player
from app.service.character_background import _named_top_skills
from tests.helpers import ROOMS_BASE, create_room, reconnect

_FULL_JSON = """{
  "summary": "总述",
  "personalDescription": "形象",
  "ideology": "信念",
  "significantPeople": "重要之人",
  "meaningfulLocations": "重要之地",
  "treasuredPossessions": "宝贵之物",
  "traits": "特质"
}"""


class _FakeCompletions:
    def __init__(self, payload: str | Exception) -> None:
        self._payload = payload
        self.calls = 0
        self.last_kwargs: dict[str, Any] = {}

    async def create(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls += 1
        self.last_kwargs = kwargs
        if isinstance(self._payload, Exception):
            raise self._payload

        class _Message:
            content = self._payload

        class _Choice:
            message = _Message()

        class _Response:
            choices = [_Choice()]

        return _Response()


def _writer_with(payload: str | Exception) -> tuple[BackgroundWriter, _FakeCompletions]:
    writer = BackgroundWriter("fake-key")
    fake = _FakeCompletions(payload)
    client: Any = type("C", (), {"chat": type("Chat", (), {"completions": fake})()})()
    writer._client = client
    return writer, fake


# ── 生成器本身 ────────────────────────────────


async def test_write_returns_all_seven_fields() -> None:
    writer, _ = _writer_with(_FULL_JSON)
    background = await writer.write("提示")
    assert background is not None
    assert background.summary == "总述"
    assert background.significantPeople == "重要之人"


async def test_write_clips_an_overlong_field() -> None:
    """模型写小作文时代码硬裁。

    🔴 上限是**兜底**：字数要求写在 prompt 里（每项 40-60 字），代码上限必须
    宽到正常输出永远碰不到。第一版设成 60 正好卡在模型的正常长度上，真实探针
    里六项有三项被切成半截句子——上限贴着期望值就不是兜底了。
    """
    long_json = f'{{"summary": "{"总" * 500}", "traits": "{"特" * 500}"}}'
    writer, _ = _writer_with(long_json)
    background = await writer.write("提示")
    assert background is not None
    assert len(background.summary) == 200
    assert len(background.traits) == 120


async def test_write_leaves_a_normal_length_field_untouched() -> None:
    """正常长度（60 字上下）的一项必须**原样**存下来——这是上面那条的另一半，
    单独立一条：只断言"超长会裁"的话，把上限改回 60 它照样绿。"""
    sentence = (
        "他相信只有自己才能保护自己，法律和警察对他来说意味着遣返和绝望，"
        "因此活在自己隐秘的道德准则里。"
    )
    writer, _ = _writer_with(f'{{"summary": "总述", "ideology": "{sentence}"}}')
    background = await writer.write("提示")
    assert background is not None
    assert background.ideology == sentence


@pytest.mark.parametrize(
    "payload",
    [
        TimeoutError("超时"),
        RuntimeError("上游 500"),
        "这不是 JSON",
        '{"summary": 12345}',
    ],
    ids=["超时", "上游报错", "不是JSON", "字段类型不对"],
)
async def test_write_degrades_to_none_on_any_failure(payload: str | Exception) -> None:
    """🔴 任何失败都是 None，不抛——它上面就是建卡，异常冒上去玩家就建不出卡。"""
    writer, _ = _writer_with(payload)
    assert await writer.write("提示") is None


def test_to_detail_drops_empty_fields_and_never_writes_injuries() -> None:
    """伤疤与恐惧症刻意不生成：开局的调查员不该自带旧伤和精神创伤，那是跑团
    过程里长出来的东西。空项也不落库——留一堆空键会让人以为填过了。"""
    detail = to_detail(CharacterBackground(summary="总述", ideology="信念", traits="   "))
    assert detail == {"ideology": "信念"}
    assert "injuries" not in detail
    assert "phobias" not in detail


def test_prompt_carries_no_scenario_content() -> None:
    """🔴 保密边界就是这个参数表：模组只进来 era/tone 两个标量。"""
    prompt = build_prompt(
        name="凌铭辉",
        occupation="记者",
        age=30,
        top_skills=[("侦察", 70)],
        era="1920 年代",
        tone="调查悬疑",
    )
    assert "1920 年代" in prompt
    assert "侦察 70" in prompt


def test_prompt_falls_back_when_the_module_has_no_era() -> None:
    prompt = build_prompt(
        name="凌铭辉", occupation="记者", age=30, top_skills=[], era=None, tone=None
    )
    assert "1920 年代美国新英格兰" in prompt


def test_named_top_skills_skips_unknown_ids() -> None:
    """查不到名字的 id 直接丢掉——把 `spot_hidden` 这样的原始 id 给模型看，
    它会把 id 当成人物特征写进背景里。"""
    named = _named_top_skills({"spot-hidden": 70, "根本不存在的技能": 99})
    assert named == [("侦察", 70)]


# ── 接线：写不出来时，卡照常成立 ────────────────────────────────


async def test_quick_build_still_works_without_a_writer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """没配 key（conftest 默认就是这个状态）→ 卡完整、背景为空。

    这正是本功能上线前的状态，#55 已验证守秘人会把空白留给玩家。
    """
    room = await create_room(client, nickname="账号昵称")
    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/quick-build",
        json={"name": "凌铭辉"},
        headers=reconnect(room["reconnectToken"]),
    )
    assert response.status_code == 201, response.text

    character = await db_session.get(Character, response.json()["data"]["characterId"])
    assert character is not None
    assert character.attributes and character.skills
    assert not character.background
    assert not character.background_detail


async def test_quick_build_survives_a_failing_writer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """🔴 生成器炸了，建卡不能跟着炸。"""
    writer, fake = _writer_with(RuntimeError("上游 500"))
    app.state.background_writer = writer

    room = await create_room(client, nickname="账号昵称")
    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/quick-build",
        json={"name": "凌铭辉"},
        headers=reconnect(room["reconnectToken"]),
    )
    assert response.status_code == 201, response.text
    assert fake.calls == 1

    character = await db_session.get(Character, response.json()["data"]["characterId"])
    assert character is not None
    assert character.attributes and character.skills
    assert not character.background


async def test_quick_build_stores_the_generated_background(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    writer, _ = _writer_with(_FULL_JSON)
    app.state.background_writer = writer

    room = await create_room(client, nickname="账号昵称")
    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/quick-build",
        json={"name": "凌铭辉"},
        headers=reconnect(room["reconnectToken"]),
    )
    assert response.status_code == 201, response.text

    character = await db_session.get(Character, response.json()["data"]["characterId"])
    assert character is not None
    assert character.background == "总述"
    assert (character.background_detail or {})["significantPeople"] == "重要之人"
    # 键名必须与前端表单一致，否则 sheet_digest 会退化成显示英文原键
    assert set(character.background_detail or {}) == {
        "personalDescription",
        "ideology",
        "significantPeople",
        "meaningfulLocations",
        "treasuredPossessions",
        "traits",
    }


async def test_ai_teammate_gets_a_background_too(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """AI 队友走同一条路径：它的有限视角里也只有职业和技能，有段过去它才
    像个人而不是一具技能表。"""
    writer, _ = _writer_with(_FULL_JSON)
    app.state.background_writer = writer

    room = await create_room(client, nickname="房主")
    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/ai-players",
        json={},
        headers=reconnect(room["reconnectToken"]),
    )
    assert response.status_code == 201, response.text

    player = await db_session.get(Player, response.json()["data"]["playerId"])
    assert player is not None and player.is_ai is True
    character = await db_session.scalar(select(Character).where(Character.player_id == player.id))
    assert character is not None
    assert character.background == "总述"
