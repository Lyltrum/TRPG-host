"""我的常用角色卡库（存 / 取 / 复用）。

## 🔴 为什么现在才有这份测试

表、DTO、四个端点、SDK 方法全都铺于 issue #77（决策 5），而 **service 层四个
函数一律 `raise not_implemented`**、前端一次都没调过——整条链每一层都在，就是
没有一个人能用到。2026-08-13 的「有消费方吗」扫描把它翻出来。

场景是线下的老玩家：这一晚开第二局、或者换个模组重开时，不想再走一遍八步向导。
**模板是复制一份新的**，不是同一个调查员带着成长回来（那是战役，另一件事）。
"""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.room import Character, Player
from tests.helpers import ROOMS_BASE, bearer, create_room, join_room, reconnect, register

TEMPLATES_BASE = "/api/v1/me/character-templates"


async def _room_with_module(client: AsyncClient, token: str) -> dict:
    """建房 + 选模组——`system_id` 是选模组那一步才写上的，而模板认它。"""
    room = await create_room(client, token=token)
    module_id = (await client.get("/api/v1/modules")).json()["data"][0]["id"]
    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/module",
        json={"moduleId": module_id, "attributeGenMethod": "point_buy"},
        headers=reconnect(room["reconnectToken"]),
    )
    return room


async def _built_character(client: AsyncClient, room: dict, name: str = "凌铭辉") -> str:
    """走「一键生成」造一张完整合法的卡，返回 characterId。"""
    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/quick-build",
        json={"name": name},
        headers=reconnect(room["reconnectToken"]),
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["data"]["characterId"]


async def _save_template(
    client: AsyncClient, token: str, character_id: str, name: str = "我的记者"
) -> dict:
    response = await client.post(
        TEMPLATES_BASE,
        json={"name": name, "characterId": character_id},
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def test_saving_and_listing_a_template(client: AsyncClient) -> None:
    token = await register(client)
    room = await _room_with_module(client, token)
    character_id = await _built_character(client, room)

    saved = await _save_template(client, token, character_id)
    assert saved["name"] == "我的记者"
    assert saved["data"]["name"] == "凌铭辉"
    assert saved["data"]["attributes"]

    listed = (await client.get(TEMPLATES_BASE, headers=bearer(token))).json()["data"]
    assert [t["templateId"] for t in listed] == [saved["templateId"]]


async def test_a_template_never_carries_this_session_wounds(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """🔴 模板只存建卡态，不许带一身伤进新局。

    keeper 改衍生值时把原值备份成 `{key}_MAX`、当前值留在 `key`
    （`deps.write_stat`），所以玩过的卡长这样：`{"HP": 3, "HP_MAX": 12}`。
    直接复制就是开局残血——而 `models/user.py` 写着模板"不带任何单局才有的
    状态（HP/理智/疯狂）"。
    """
    token = await register(client)
    room = await _room_with_module(client, token)
    character_id = await _built_character(client, room)

    # 模拟这一局里挨过打、掉过 SAN
    character = await db_session.get(Character, character_id)
    assert character is not None
    full = dict(character.derived_stats or {})
    character.derived_stats = {
        **full,
        "HP": 3,
        "HP_MAX": full["HP"],
        "SAN": 20,
        "SAN_MAX": full["SAN"],
    }
    await db_session.commit()

    saved = await _save_template(client, token, character_id)

    assert saved["data"]["derived_stats"]["HP"] == full["HP"]
    assert saved["data"]["derived_stats"]["SAN"] == full["SAN"]
    # `_MAX` 备份键本身也不该留在模板里——它是"被改过"的痕迹，不是建卡态
    assert not any(k.endswith("_MAX") for k in saved["data"]["derived_stats"])


async def test_reusing_a_template_copies_it_into_a_fresh_draft(client: AsyncClient) -> None:
    """🔴 第二局：拿常用卡建卡，属性技能整份带过来，而且**直接建完**。

    2026-08-13 真人反馈：「我选择自己常用的角色卡之后，为什么还要我进行下一步
    呀？」——卡库里那张本来就是建完的卡，再走一遍八步向导唯一的产出是把刚选的
    东西原样确认一次。校验没少做，只是改由后端当场跑（见下一条：没过就退回
    草稿）。
    """
    token = await register(client)
    first = await _room_with_module(client, token)
    character_id = await _built_character(client, first)
    template = await _save_template(client, token, character_id)

    second = await _room_with_module(client, token)
    response = await client.post(
        f"{ROOMS_BASE}/{second['roomId']}/characters",
        json={"basedOnTemplateId": template["templateId"]},
        headers=reconnect(second["reconnectToken"]),
    )

    assert response.status_code in (200, 201), response.text
    draft = response.json()["data"]
    assert draft["status"] == "complete"

    got = await client.get(
        f"{ROOMS_BASE}/{second['roomId']}/characters/{draft['characterId']}",
        headers=reconnect(second["reconnectToken"]),
    )
    assert got.json()["data"]["name"] == "凌铭辉"
    assert got.json()["data"]["attributes"] == template["data"]["attributes"]


async def test_a_template_that_is_illegal_here_falls_back_to_a_draft(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """🔴 「直接建完」不等于「跳过校验」：模板存的时候合法，不代表在**这个**
    房间的规则下还合法（换规则系统、规则表改过）。

    校验没过就落成 `draft`，让玩家进向导把不合法的地方改掉——那时候向导干的是
    "修一张具体哪里不合法的卡"，不是"再确认一遍"。**这一条不许退化成把错误
    直接抛给玩家**：草稿已经建出来了，抛异常等于让他连修的入口都没有。
    """
    token = await register(client)
    first = await _room_with_module(client, token)
    template = await _save_template(client, token, await _built_character(client, first))

    # 把模板改成一张必然违规的卡（技能远超上限），模拟"这里不合法"
    from app.models.user import UserCharacterTemplate

    row = await db_session.get(UserCharacterTemplate, template["templateId"])
    assert row is not None
    row.data = {**row.data, "skills": {**row.data["skills"], "dodge": 99999}}
    await db_session.commit()

    second = await _room_with_module(client, token)
    response = await client.post(
        f"{ROOMS_BASE}/{second['roomId']}/characters",
        json={"basedOnTemplateId": template["templateId"]},
        headers=reconnect(second["reconnectToken"]),
    )

    assert response.status_code in (200, 201), response.text
    assert response.json()["data"]["status"] == "draft"


async def test_the_copy_does_not_write_back_to_the_library(client: AsyncClient) -> None:
    """**复制，不是引用**：第二局怎么玩坏都不回头改卡库里那张。"""
    token = await register(client)
    first = await _room_with_module(client, token)
    template = await _save_template(client, token, await _built_character(client, first))

    second = await _room_with_module(client, token)
    draft = (
        await client.post(
            f"{ROOMS_BASE}/{second['roomId']}/characters",
            json={"basedOnTemplateId": template["templateId"]},
            headers=reconnect(second["reconnectToken"]),
        )
    ).json()["data"]
    await client.patch(
        f"{ROOMS_BASE}/{second['roomId']}/characters/{draft['characterId']}",
        json={"name": "改了个名"},
        headers=reconnect(second["reconnectToken"]),
    )

    still = (
        await client.get(f"{TEMPLATES_BASE}/{template['templateId']}", headers=bearer(token))
    ).json()["data"]
    assert still["data"]["name"] == "凌铭辉"


async def test_editing_a_template_changes_only_the_text(client: AsyncClient) -> None:
    """卡库详情页就地改文字：卡名 + 姓名/性别/居住地/背景。

    `data` 是**部分更新**——只合并请求里给了的键。整份覆盖的话，前端少传一个
    字段就等于把它清空了。
    """
    token = await register(client)
    room = await _room_with_module(client, token)
    template = await _save_template(client, token, await _built_character(client, room))
    before = template["data"]

    response = await client.patch(
        f"{TEMPLATES_BASE}/{template['templateId']}",
        json={
            "name": "跑长期的那张",
            "data": {"name": "凌铭辉（改）", "gender": "男", "background": "他是个记者。"},
        },
        headers=bearer(token),
    )

    assert response.status_code == 200, response.text
    updated = response.json()["data"]
    assert updated["name"] == "跑长期的那张"
    assert updated["data"]["name"] == "凌铭辉（改）"
    assert updated["data"]["gender"] == "男"
    assert updated["data"]["background"] == "他是个记者。"
    # 没给的键原样留着——尤其是规则数
    assert updated["data"]["attributes"] == before["attributes"]
    assert updated["data"]["skills"] == before["skills"]
    assert updated["data"]["age"] == before["age"]


async def test_rule_numbers_are_refused_not_silently_dropped(client: AsyncClient) -> None:
    """🔴 属性/技能/年龄这些规则数**显式拒绝**，不静默丢弃。

    静默丢弃的话，前端以为改上去了、界面上也显示改了，刷新一下又变回原样——
    这种 bug 前后端两头都不会变红。

    （改数值本身有一条走得通的路：用这张卡开局 → 在向导里改 → 再存一张。
    加一道门必须同时给它配一条修法。）
    """
    token = await register(client)
    room = await _room_with_module(client, token)
    template = await _save_template(client, token, await _built_character(client, room))

    response = await client.patch(
        f"{TEMPLATES_BASE}/{template['templateId']}",
        json={"data": {"age": 60, "attributes": {"STR": 99}}},
        headers=bearer(token),
    )

    assert response.status_code == 422, response.text
    message = response.json()["error"]["message"]
    assert "age" in message and "attributes" in message

    unchanged = (
        await client.get(f"{TEMPLATES_BASE}/{template['templateId']}", headers=bearer(token))
    ).json()["data"]
    assert unchanged["data"]["age"] == template["data"]["age"]


async def test_someone_elses_template_cannot_be_edited(client: AsyncClient) -> None:
    """改别人的卡跟读别人的卡同一个待遇：404，不泄露"这个 id 存在"。"""
    owner = await register(client)
    room = await _room_with_module(client, owner)
    template = await _save_template(client, owner, await _built_character(client, room))

    stranger = await register(client)
    response = await client.patch(
        f"{TEMPLATES_BASE}/{template['templateId']}",
        json={"name": "我的了"},
        headers=bearer(stranger),
    )
    assert response.status_code == 404


async def test_someone_elses_template_is_not_found(client: AsyncClient) -> None:
    """🔴 模板 id 是 uuid，但"猜不到"不是访问控制。"""
    owner = await register(client)
    room = await _room_with_module(client, owner)
    template = await _save_template(client, owner, await _built_character(client, room))

    stranger = await register(client)
    response = await client.get(
        f"{TEMPLATES_BASE}/{template['templateId']}", headers=bearer(stranger)
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_deleting_a_template(client: AsyncClient) -> None:
    token = await register(client)
    room = await _room_with_module(client, token)
    template = await _save_template(client, token, await _built_character(client, room))

    response = await client.delete(
        f"{TEMPLATES_BASE}/{template['templateId']}", headers=bearer(token)
    )
    assert response.status_code == 200, response.text
    assert (await client.get(TEMPLATES_BASE, headers=bearer(token))).json()["data"] == []


async def test_a_player_without_an_account_gets_a_clear_reason(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """🔴 `Player.user_id` 可空。卡库是账号级的——没账号就没卡库。

    要**说清楚是为什么**，不能让它掉进含糊的"常用卡不存在"里（禁止静默兜底
    那一族）。这里直接把 `user_id` 抹掉来走到那条分支：账号身份贯通（`#106`）
    之后正常入房都带账号，但那一列仍然可空，分支就仍然到得了。
    """
    token = await register(client)
    room = await _room_with_module(client, token)
    template = await _save_template(client, token, await _built_character(client, room))

    guest_token = await register(client)
    guest = await join_room(client, room["roomCode"], guest_token)
    player = await db_session.scalar(
        select(Player).where(Player.reconnect_token == guest["reconnectToken"])
    )
    assert player is not None
    player.user_id = None
    await db_session.commit()

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters",
        json={"basedOnTemplateId": template["templateId"]},
        headers=reconnect(guest["reconnectToken"]),
    )

    assert response.status_code == 404
    assert "登录" in response.json()["error"]["message"]
