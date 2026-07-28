"""新增的三块建卡能力（迁移自用户个人项目 coc-char-gen，见
docs/character-build-migration/design.md）的端点测试：
- 年龄调整（apply-age-adjustment）
- 掷点池生成法（roll-attribute-pool）
- 结构化背景故事（background_detail）
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.room import Character
from tests.helpers import ROOMS_BASE, create_room, reconnect

BASE_ATTRIBUTES = {
    "STR": 60,
    "CON": 60,
    "POW": 60,
    "DEX": 60,
    "APP": 60,
    "SIZ": 60,
    "INT": 60,
    "EDU": 80,
    "LUCK": 50,
}

MINIMAL_CHARACTER_UPDATE = {
    "name": "无业游民",
    "attributes": BASE_ATTRIBUTES,
    "derivedStats": {},
    "skills": {},
    "equipment": [],
    "occupation": None,
    "background": "",
    "notes": "",
}


async def _create_draft(client: AsyncClient, room: dict) -> str:
    draft = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters", headers=reconnect(room["reconnectToken"])
    )
    assert draft.status_code == 201
    return draft.json()["data"]["characterId"]


# ── 年龄调整 ────────────────────────────────────────────────────────────────


async def test_apply_age_adjustment_requires_attributes_first(client: AsyncClient) -> None:
    """还没生成过属性（掷骰/点数购买/掷点池都没跑过）就调这个接口要拒——
    没有可扣减的对象。"""
    room = await create_room(client)
    character_id = await _create_draft(client, room)

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/apply-age-adjustment",
        json={"age": 45},
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ATTRIBUTES_NOT_SET"


async def test_apply_age_adjustment_middle_age_band(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """40-49 档：EDU 改进检定 ×2（结果随机，但结构必须对）、STR/CON/DEX 合计
    -5（确定性分摊）、APP -5（确定性）、MOV 惩罚 -1，且真的写回了数据库。"""
    room = await create_room(client)
    character_id = await _create_draft(client, room)
    await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        json=MINIMAL_CHARACTER_UPDATE,
        headers=reconnect(room["reconnectToken"]),
    )

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/apply-age-adjustment",
        json={"age": 45},
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]

    assert data["age"] == 45
    assert data["ageLabel"] == "40–49"
    assert len(data["eduChecks"]) == 2
    assert data["eduFlatAdjustment"] == 0
    assert data["scdLoss"] == 5
    assert set(data["scdAffectedAttributes"]) == {"STR", "CON", "DEX"}
    assert data["appLoss"] == 5
    assert data["luckRerolled"] is False
    assert data["movPenalty"] == 1

    before = data["attributesBefore"]
    after = data["attributesAfter"]
    assert before == BASE_ATTRIBUTES
    # APP 减值是确定性的（不涉及掷骰）。
    assert after["APP"] == before["APP"] - 5
    # STR+CON+DEX 合计确定性地减 5（分摊算法本身不随机，只是具体分到哪项
    # 不重要，这里只断言总量）。
    assert (before["STR"] + before["CON"] + before["DEX"]) - (
        after["STR"] + after["CON"] + after["DEX"]
    ) == 5
    # LUCK 不受影响（这一档没有幸运双掷）。
    assert after["LUCK"] == before["LUCK"]
    # EDU 改进检定的结果是随机的，但只可能不变或变大，且不会超过 99。
    assert before["EDU"] <= after["EDU"] <= 99

    # 真的落库了：年龄和调整后的属性都要能读回来。
    character = await db_session.get(Character, character_id)
    assert character is not None
    assert character.age == 45
    assert character.attributes == after


async def test_apply_age_adjustment_youth_band_is_fully_deterministic(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """15-19 档没有 EDU 改进检定（0 次），只有固定 -5——这一档除了幸运双掷
    以外全部确定性，用来验证 EDU/STR+SIZ 减值算得对。"""
    room = await create_room(client)
    character_id = await _create_draft(client, room)
    await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        json=MINIMAL_CHARACTER_UPDATE,
        headers=reconnect(room["reconnectToken"]),
    )

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/apply-age-adjustment",
        json={"age": 17},
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]

    assert data["ageLabel"] == "15–19"
    assert data["eduChecks"] == []
    assert data["eduFlatAdjustment"] == -5
    assert data["scdLoss"] == 5
    assert set(data["scdAffectedAttributes"]) == {"STR", "SIZ"}
    assert data["luckRerolled"] is True

    before = data["attributesBefore"]
    after = data["attributesAfter"]
    # EDU 固定 -5，没有随机成分。
    assert after["EDU"] == before["EDU"] - 5
    # 青年档只扣 STR/SIZ，不动 CON/DEX。
    assert after["CON"] == before["CON"]
    assert after["DEX"] == before["DEX"]
    assert (before["STR"] + before["SIZ"]) - (after["STR"] + after["SIZ"]) == 5
    # 幸运双掷取高：结果必须是合法的 3d6*5 产出（[15, 90] 且 5 的倍数）。
    assert 15 <= after["LUCK"] <= 90
    assert after["LUCK"] % 5 == 0


# ── 掷点池生成法 ────────────────────────────────────────────────────────────


async def test_roll_attribute_pool_writes_total_and_generation_method(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    room = await create_room(client)
    character_id = await _create_draft(client, room)

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/roll-attribute-pool",
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data["rolls"]) == 8
    assert sum(roll["value"] for roll in data["rolls"]) == data["total"]
    # 5 次 3d6*5（[15,90]）+ 3 次 (2d6+6)*5（[40,90]），理论范围 195–720。
    assert 195 <= data["total"] <= 720

    character = await db_session.get(Character, character_id)
    assert character is not None
    assert character.generation_method == "roll_pool"
    assert character.attribute_pool_total == data["total"]
    # 掷点池不直接写 attributes——分配是后续 PATCH 完成的。
    assert character.attributes is None


def _distribute_pool_total(total: int, keys: list[str]) -> dict[str, int]:
    """把一个池子总值机械地分配到给定的属性键上：每项先给最低 15，剩余部分
    以 5 点为单位轮转分配、不超过单项上限 90——只是构造一份满足
    `[15,90]`+5 的倍数+总和精确匹配这三条约束的测试数据，不代表任何真实
    分配策略。"""
    values = dict.fromkeys(keys, 15)
    remaining = total - 15 * len(keys)
    assert remaining % 5 == 0
    i = 0
    while remaining > 0:
        key = keys[i % len(keys)]
        if values[key] < 90:
            values[key] += 5
            remaining -= 5
        i += 1
    return values


async def test_roll_attribute_pool_then_matching_allocation_completes(
    client: AsyncClient,
) -> None:
    """掷池子 → 按总值分配 → 保存 → complete 应该成功（分配总和精确匹配）。
    不选职业、不加技能点，绕开跟本次改动无关的技能预算校验，只验证掷点池
    这条生成方法本身的校验通路。"""
    room = await create_room(client)
    character_id = await _create_draft(client, room)

    rolled = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/roll-attribute-pool",
        headers=reconnect(room["reconnectToken"]),
    )
    total = rolled.json()["data"]["total"]

    point_buy_keys = ["STR", "CON", "DEX", "APP", "POW", "SIZ", "INT", "EDU"]
    attributes = _distribute_pool_total(total, point_buy_keys)
    attributes["LUCK"] = 50

    await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        json={**MINIMAL_CHARACTER_UPDATE, "attributes": attributes},
        headers=reconnect(room["reconnectToken"]),
    )

    completed = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/complete",
        headers=reconnect(room["reconnectToken"]),
    )

    assert completed.status_code == 200, completed.text


async def test_roll_attribute_pool_then_mismatched_allocation_is_rejected(
    client: AsyncClient,
) -> None:
    """分配总和跟掷出来的池子总值差 5 点，complete 必须拒绝——不能只信任
    客户端报的分配结果。"""
    room = await create_room(client)
    character_id = await _create_draft(client, room)

    rolled = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/roll-attribute-pool",
        headers=reconnect(room["reconnectToken"]),
    )
    total = rolled.json()["data"]["total"]

    point_buy_keys = ["STR", "CON", "DEX", "APP", "POW", "SIZ", "INT", "EDU"]
    attributes = _distribute_pool_total(total, point_buy_keys)
    # 从某一项偷 5 点出来，让总和比池子总值少 5——只要那一项还留有余量
    # （分配算法保证至少有一项 > 15，因为 total 远大于 8*15=120）。
    bumped_key = next(key for key in point_buy_keys if attributes[key] > 15)
    attributes[bumped_key] -= 5
    attributes["LUCK"] = 50

    await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        json={**MINIMAL_CHARACTER_UPDATE, "attributes": attributes},
        headers=reconnect(room["reconnectToken"]),
    )

    completed = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/complete",
        headers=reconnect(room["reconnectToken"]),
    )

    assert completed.status_code == 422
    codes = [issue["code"] for issue in completed.json()["error"]["details"]]
    assert "ATTRIBUTE_POOL_MISMATCH" in codes


# ── 结构化背景故事 ──────────────────────────────────────────────────────────


async def test_background_detail_round_trips_through_patch_and_get(
    client: AsyncClient,
) -> None:
    room = await create_room(client)
    character_id = await _create_draft(client, room)

    background_detail = {
        "personalDescription": "沉默寡言的图书管理员",
        "ideology": "真相高于一切",
        "significantPeople": "已故的导师",
        "meaningfulLocations": "米斯卡塔尼克大学图书馆",
        "treasuredPossessions": "一本手抄笔记",
        "traits": "过度谨慎",
        "injuries": "",
        "phobias": "幽闭恐惧",
    }

    await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        json={**MINIMAL_CHARACTER_UPDATE, "backgroundDetail": background_detail},
        headers=reconnect(room["reconnectToken"]),
    )

    response = await client.get(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 200
    assert response.json()["data"]["backgroundDetail"] == background_detail


async def test_background_detail_defaults_to_none(client: AsyncClient) -> None:
    """不传 backgroundDetail 时不该伪造一个空字典出来——None 就是没填过。"""
    room = await create_room(client)
    character_id = await _create_draft(client, room)

    await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        json=MINIMAL_CHARACTER_UPDATE,
        headers=reconnect(room["reconnectToken"]),
    )

    response = await client.get(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.json()["data"]["backgroundDetail"] is None
