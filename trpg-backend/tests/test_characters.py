from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.seed import BUILTIN_GAME_ID
from app.models.content import GameSystem
from app.models.room import Character, Player, Room
from tests.helpers import ROOMS_BASE, create_room, join_room, reconnect, register

# 「存在但没配规则数据」的规则系统，只出现在测试里（见文件末尾用例的说明）。
UNCONFIGURED_SYSTEM_ID = "00000000-0000-0000-0000-0000000000fe"

BUILT_CHARACTER = {
    "name": "陈探员",
    "attributes": {
        "STR": 50,
        "CON": 60,
        "POW": 55,
        "DEX": 45,
        "APP": 50,
        "SIZ": 60,
        "INT": 70,
        "EDU": 80,
        "LUCK": 50,
    },
    "derivedStats": {"HP": 12, "SAN": 55, "MP": 11},
    # 私家侦探（occupation id=16）的两项职业技能，用真实技能 id 键控、分配在
    # 职业技能点预算内（EDU*2+STR*2=80*2+50*2=260）——issue #84 S2 起
    # complete 会用 coc7_rules 权威校验，键必须是技能表里的 id 不是技能名。
    # credit-rating（信用评级，PR #85 review #4 新增为一条真正的技能）落在
    # 私家侦探信用区间 [9,40] 内，否则 complete 会被 CREDIT_OUT_OF_RANGE 拒。
    "skills": {"law": 55, "spot-hidden": 75, "credit-rating": 25},
    "equipment": [{"name": "左轮手枪"}, {"name": "手电筒"}],
    "occupation": "私家侦探",
    "background": "曾是警察",
    "notes": "",
}


async def test_full_character_build_flow_marks_player_ready(client: AsyncClient) -> None:
    room = await create_room(client)

    draft = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters", headers=reconnect(room["reconnectToken"])
    )
    assert draft.status_code == 201
    character_id = draft.json()["data"]["characterId"]
    assert draft.json()["data"]["status"] == "draft"

    saved = await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        json=BUILT_CHARACTER,
        headers=reconnect(room["reconnectToken"]),
    )
    assert saved.status_code == 200

    completed = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/complete",
        headers=reconnect(room["reconnectToken"]),
    )
    assert completed.status_code == 200

    preview = await client.get(f"{ROOMS_BASE}/{room['roomCode']}")
    host = next(p for p in preview.json()["data"]["players"] if p["isHost"])
    assert host["hasCharacter"] is True


async def test_create_character_requires_token(client: AsyncClient) -> None:
    room = await create_room(client)

    response = await client.post(f"{ROOMS_BASE}/{room['roomId']}/characters")

    assert response.status_code == 401


async def test_cannot_edit_another_players_character(client: AsyncClient) -> None:
    room = await create_room(client)
    joined = await join_room(client, room["roomCode"], await register(client))
    draft = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters", headers=reconnect(room["reconnectToken"])
    )
    character_id = draft.json()["data"]["characterId"]

    response = await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        json=BUILT_CHARACTER,
        headers=reconnect(joined["reconnectToken"]),
    )

    assert response.status_code == 403


async def test_character_not_found_returns_404(client: AsyncClient) -> None:
    room = await create_room(client)

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/missing/complete",
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 404


async def test_complete_character_rejects_invalid_card(client: AsyncClient) -> None:
    """issue #84 S2：complete 前用 coc7_rules 权威校验，非法卡（这里让职业
    技能点花费远超预算）直接被拒，返回结构化校验报告，而不是被静默放行。"""
    room = await create_room(client)
    draft = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters", headers=reconnect(room["reconnectToken"])
    )
    character_id = draft.json()["data"]["characterId"]

    invalid_character = {
        **BUILT_CHARACTER,
        # 私家侦探的全部 8 项职业技能都拉满到上限 99，总花费（>600）远超
        # 职业预算 260 + 兴趣预算 140 = 400 的总预算，应该触发
        # SKILL_POINTS_EXCEEDED（查的是总预算，见 coc7_rules 那处注释）。
        "skills": {
            "disguise": 99,
            "drive-auto": 99,
            "fighting-brawl": 99,
            "fast-talk": 99,
            "law": 99,
            "listen": 99,
            "persuade": 99,
            "spot-hidden": 99,
        },
    }
    saved = await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        json=invalid_character,
        headers=reconnect(room["reconnectToken"]),
    )
    assert saved.status_code == 200

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/complete",
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "CHARACTER_INVALID"
    codes = [issue["code"] for issue in body["error"]["details"]]
    assert "SKILL_POINTS_EXCEEDED" in codes


# ── 角色卡读回：后端是唯一事实来源（issue #96）─────────────────────────


async def test_get_character_reads_back_saved_card(client: AsyncClient) -> None:
    """保存后能从后端把角色卡读回来。

    补这个端点是为了让客户端不必再把角色卡存进 localStorage 当权威源——
    那份本地副本的结构会随后端 schema 演进而过期（PR #88 加幸运后，本地存的
    8 键旧卡就再也编辑不了了）。
    """
    room = await create_room(client)
    draft = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters", headers=reconnect(room["reconnectToken"])
    )
    character_id = draft.json()["data"]["characterId"]
    await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        json=BUILT_CHARACTER,
        headers=reconnect(room["reconnectToken"]),
    )

    response = await client.get(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["id"] == character_id
    assert data["name"] == BUILT_CHARACTER["name"]
    assert data["attributes"] == BUILT_CHARACTER["attributes"]
    assert data["occupation"] == BUILT_CHARACTER["occupation"]
    # 默认是点数购买法——迁移前建的卡和前端建卡向导走的都是这条路径
    assert data["generationMethod"] == "pointbuy"


async def test_list_party_characters_shows_teammates(client: AsyncClient) -> None:
    """队友卡（exec/14 P5.3）：方向与 P5.2 相反的一条——**放开**。

    真人桌上角色卡摊在桌面互相传阅，此前系统只有「读回自己那张」，反而比
    真人桌还封闭（exec/18 ⑨）。⑦检定与⑧HP/SAN 已裁决为公开，不做脱敏。
    """
    host = await create_room(client)
    guest_token = await register(client, "party_guest")
    guest = await join_room(client, host["roomCode"], guest_token, nickname="访客")

    draft = await client.post(
        f"{ROOMS_BASE}/{host['roomId']}/characters", headers=reconnect(host["reconnectToken"])
    )
    character_id = draft.json()["data"]["characterId"]
    await client.patch(
        f"{ROOMS_BASE}/{host['roomId']}/characters/{character_id}",
        json=BUILT_CHARACTER,
        headers=reconnect(host["reconnectToken"]),
    )

    # 访客拿自己的房间凭证去看房主的卡——这正是此前被 `_get_own_character` 拒掉的
    response = await client.get(
        f"{ROOMS_BASE}/{host['roomId']}/characters",
        headers=reconnect(guest["reconnectToken"]),
    )
    assert response.status_code == 200
    party = response.json()["data"]
    assert [p["playerId"] for p in party] == [host["playerId"], guest["playerId"]]

    owner = party[0]
    assert owner["name"] == BUILT_CHARACTER["name"]
    assert owner["attributes"] == BUILT_CHARACTER["attributes"]
    # ⑦⑧ 公开：HP/SAN 与技能都不脱敏
    assert owner["derivedStats"]["HP"] == 12
    assert owner["derivedStats"]["SAN"] == 55
    assert owner["skills"] == BUILT_CHARACTER["skills"]
    # 建卡过程字段不外泄——那是卡主自己的建卡向导才需要的东西
    assert "generationMethod" not in owner
    assert "allocatedAttributes" not in owner
    assert "notes" not in owner

    # 还没建卡的人也要出现：「谁还没准备好」正是队伍面板要回答的问题
    assert party[1]["status"] == "absent"
    assert party[1]["nickname"] == "访客"
    assert party[1]["name"] is None


async def test_list_party_characters_rejects_outsider(client: AsyncClient) -> None:
    """不在这个房间里的人拿不到任何一张卡。"""
    host = await create_room(client)
    other_token = await register(client, "party_outsider")
    other_room = await create_room(client, other_token)

    response = await client.get(
        f"{ROOMS_BASE}/{host['roomId']}/characters",
        headers=reconnect(other_room["reconnectToken"]),
    )
    assert response.status_code == 403


async def test_roll_attributes_marks_card_as_rolled(client: AsyncClient) -> None:
    """服务端权威掷骰后，这张卡要被标记成 roll。

    complete 时据此跳过点数购买法的总预算校验——掷骰结果 8 项总和均值约 457、
    范围 195–720，拿 480 的预算去卡它会把合法的卡判成非法。
    """
    room = await create_room(client)
    draft = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters", headers=reconnect(room["reconnectToken"])
    )
    character_id = draft.json()["data"]["characterId"]

    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/roll-attributes",
        headers=reconnect(room["reconnectToken"]),
    )

    read_back = await client.get(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        headers=reconnect(room["reconnectToken"]),
    )
    assert read_back.json()["data"]["generationMethod"] == "roll"


async def test_replacing_rolled_attributes_falls_back_to_point_buy(client: AsyncClient) -> None:
    """🔴 掷完骰再自己重填属性，来源标记必须退回点数购买法（PR #97 review [1]）。

    `roll` 这个标记的意义是「这组属性是服务端掷出来的」，据此跳过 480 点预算
    校验。但 PATCH 接受客户端任意属性——标记原样留着的话，先掷一次、再把 8 项
    全顶到单项上限 90（8 项共 720 点、远超 480）提交，complete 就会走 roll
    分支放行。用 90 而不是 99 是为了精确命中预算这条校验——99 会先被单项
    区间 [10, 90] 拦下，测出来的就不是预算有没有生效了。
    """
    room = await create_room(client)
    draft = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters", headers=reconnect(room["reconnectToken"])
    )
    character_id = draft.json()["data"]["characterId"]
    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/roll-attributes",
        headers=reconnect(room["reconnectToken"]),
    )

    maxed = {
        **BUILT_CHARACTER,
        # 幸运不参与点数购买，保持原值；其余 8 项顶到单项上限。
        "attributes": {key: (50 if key == "LUCK" else 90) for key in BUILT_CHARACTER["attributes"]},
    }
    await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        json=maxed,
        headers=reconnect(room["reconnectToken"]),
    )

    read_back = await client.get(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        headers=reconnect(room["reconnectToken"]),
    )
    assert read_back.json()["data"]["generationMethod"] == "pointbuy"

    completed = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/complete",
        headers=reconnect(room["reconnectToken"]),
    )
    assert completed.status_code == 422
    codes = [issue["code"] for issue in completed.json()["error"]["details"]]
    assert "ATTRIBUTE_POINTS_EXCEEDED" in codes


async def test_saving_a_rolled_card_unchanged_keeps_roll_method(client: AsyncClient) -> None:
    """上一条的对照：属性原样存回去时，`roll` 必须保住。

    建卡向导的 PATCH 是整卡保存，掷骰法那条路径也要走它把姓名/技能存下来。
    若一见到 PATCH 就退回 pointbuy，掷出来总和超 480 的合法卡会被判非法——
    等于废掉 roll-attributes。只测上一条会漏掉这个方向。
    """
    room = await create_room(client)
    draft = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters", headers=reconnect(room["reconnectToken"])
    )
    character_id = draft.json()["data"]["characterId"]
    rolled = (
        await client.post(
            f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/roll-attributes",
            headers=reconnect(room["reconnectToken"]),
        )
    ).json()["data"]["attributes"]

    await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        json={**BUILT_CHARACTER, "attributes": rolled},
        headers=reconnect(room["reconnectToken"]),
    )

    read_back = await client.get(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        headers=reconnect(room["reconnectToken"]),
    )
    assert read_back.json()["data"]["generationMethod"] == "roll"


async def test_cannot_read_another_players_character(client: AsyncClient) -> None:
    """角色卡里有背景故事/装备这类属于该玩家的信息，别人不能直接拉。"""
    room = await create_room(client)
    joined = await join_room(client, room["roomCode"], await register(client))
    draft = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters", headers=reconnect(room["reconnectToken"])
    )
    character_id = draft.json()["data"]["characterId"]

    response = await client.get(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        headers=reconnect(joined["reconnectToken"]),
    )

    assert response.status_code == 403


async def test_complete_character_rejects_unconfigured_ruleset(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """PR #113 review：房间的规则系统没有规则数据时，complete 必须拒绝。

    这是参数注入引入的**回归**，不是既有缺陷：改之前属性键来自 `coc7_rules` 的
    模块常量 `COC7_ATTRIBUTE_KEYS`（写死 9 项），空白角色卡必然触发"缺少 9 个
    属性"；改成从传入的 `RulesetRead` 取之后，空目录退化成"零个约束"，
    `validate_character` 一条问题都查不出来，整张空白卡会被标记成建卡完成。

    用例特意让角色卡**完全空白**（不 PATCH 任何数据），因为这正是回归最严重的
    形态：连名字属性职业都没有的卡被判合法。
    """
    room = await create_room(client)
    draft = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters", headers=reconnect(room["reconnectToken"])
    )
    character_id = draft.json()["data"]["characterId"]

    # 把房间挂到一个没有规则数据的系统上。走不了 select_module（那要求模组存在
    # 并绑定系统），直接改库——这条路径现实中来自"未来的自定义规则系统"，
    # 而不是当前任何一个用户操作。
    db_session.add(
        GameSystem(
            id=UNCONFIGURED_SYSTEM_ID,
            game_id=BUILTIN_GAME_ID,
            name="未配置规则的系统",
            ruleset=None,
        )
    )
    db_room = await db_session.get(Room, room["roomId"])
    assert db_room is not None
    db_room.system_id = UNCONFIGURED_SYSTEM_ID
    await db_session.commit()

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/complete",
        headers=reconnect(room["reconnectToken"]),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "RULESET_NOT_CONFIGURED"


async def test_completing_a_character_renames_the_player_to_the_character_name(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """建完卡之后，这个房间里的显示名要变成**角色名**（真人实测反馈）。

    玩家给调查员起了名字，守秘人却还在用账号昵称叫他——桌上没人会用登录名
    称呼你的角色。`Player.nickname` 是全链路唯一的称呼来源（原话广播 / 守秘人
    roster 与历史行 / 检定请求 / 队友列表都读它），所以断言落在它上面。
    """
    room = await create_room(client, nickname="账号昵称")
    draft = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters", headers=reconnect(room["reconnectToken"])
    )
    character_id = draft.json()["data"]["characterId"]
    await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        json={
            "name": "凌铭辉",
            "attributes": BUILT_CHARACTER["attributes"],
            "derivedStats": {},
            "skills": {},
            "equipment": [],
            "occupation": None,
            "background": "",
            "notes": "",
        },
        headers=reconnect(room["reconnectToken"]),
    )
    completed = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/complete",
        headers=reconnect(room["reconnectToken"]),
    )
    assert completed.status_code == 200, completed.text

    player = await db_session.get(Player, room["playerId"])
    assert player is not None
    assert player.nickname == "凌铭辉"


async def test_completing_a_nameless_character_keeps_the_old_nickname(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """角色名是空的（老卡/异常数据）时不要把显示名清成空串——空名字比账号名更糟。"""
    room = await create_room(client, nickname="账号昵称")
    draft = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters", headers=reconnect(room["reconnectToken"])
    )
    character_id = draft.json()["data"]["characterId"]
    await client.patch(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}",
        json={
            "name": "   ",
            "attributes": BUILT_CHARACTER["attributes"],
            "derivedStats": {},
            "skills": {},
            "equipment": [],
            "occupation": None,
            "background": "",
            "notes": "",
        },
        headers=reconnect(room["reconnectToken"]),
    )
    await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/{character_id}/complete",
        headers=reconnect(room["reconnectToken"]),
    )

    player = await db_session.get(Player, room["playerId"])
    assert player is not None
    assert player.nickname == "账号昵称"


# ── 一键生成（零基础玩家的第二条建卡路径）────────────


async def test_quick_build_produces_a_complete_and_valid_character(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """填个名字就能开局：生成的卡必须是**完成态**且**规则上合法**。

    合法性不靠这条用例的断言保证——`roll_character_sheet` 内部跑的是与
    `complete_character` 同一套 `validate_character_with_occupation`，不合法
    会直接抛。这里断言的是"它确实落库了、状态对、玩家被标记成已建卡"。
    """
    room = await create_room(client, nickname="账号昵称")
    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/quick-build",
        json={"name": "凌铭辉"},
        headers=reconnect(room["reconnectToken"]),
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["status"] == "complete"

    character = await db_session.get(Character, response.json()["data"]["characterId"])
    assert character is not None
    assert character.name == "凌铭辉"
    assert character.occupation_id is not None
    assert character.attributes and character.skills
    assert (character.derived_stats or {}).get("HP")
    # 生成器固定 30 岁：该区间无年龄修正，两份属性天然相同
    assert character.allocated_attributes == character.attributes

    player = await db_session.get(Player, room["playerId"])
    assert player is not None
    assert player.has_character is True
    # 跟走完向导一样，显示名换成角色名
    assert player.nickname == "凌铭辉"


async def test_quick_build_rejects_a_blank_name(client: AsyncClient) -> None:
    """名字必填、不做兜底——它是代入感的落点，静默塞个"无名调查员"只会让人
    以为坏了。"""
    room = await create_room(client)
    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/quick-build",
        json={"name": "   "},
        headers=reconnect(room["reconnectToken"]),
    )
    assert response.status_code == 422


async def test_quick_build_reuses_the_existing_draft_row(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """先点了「创建角色」建了草稿、又改主意点一键生成——不能留下两张卡。

    留两张的后果不是多一行数据：`_get_own_character` 之外的地方按
    (room_id, player_id) 查卡，两张会让"我的卡是哪张"变成掷硬币。
    """
    room = await create_room(client)
    draft = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters", headers=reconnect(room["reconnectToken"])
    )
    draft_id = draft.json()["data"]["characterId"]

    response = await client.post(
        f"{ROOMS_BASE}/{room['roomId']}/characters/quick-build",
        json={"name": "阿玲"},
        headers=reconnect(room["reconnectToken"]),
    )
    assert response.json()["data"]["characterId"] == draft_id

    rows = await db_session.execute(select(Character).where(Character.room_id == room["roomId"]))
    assert len(list(rows.scalars())) == 1
