"""Service 层：房间 + 模组 + 游戏目录的数据访问和业务操作。

issue #77 之前是内存字典 stub，本期切换为对 `rooms`/`players`/`games`/
`game_systems`/`scenarios`/`events` 等表的真实 SQLAlchemy 读写——进程重启后
房间/玩家数据不再丢失。角色卡相关操作已拆到 service/character.py（issue #77
决策：`auth`/`room`/`character`/`ws` 四个 service 各自独立切换）。
"""

import secrets
import string
from datetime import UTC, datetime

from fastapi import status
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import table_state
from app.core.db import Base
from app.core.errors import AppException, ErrorCode
from app.core.keeper.capabilities.presence import state as presence_state
from app.core.narration.contract import NarrationContext
from app.dto.game import GameRead, GameSystemRead, RulesetRead
from app.dto.module import ModuleDetailRead
from app.dto.replay import ReplayEventRead, RoomSummaryRead
from app.dto.room import (
    JoinRoomBody,
    LastSessionRead,
    ModuleRead,
    MyRoomSummary,
    RoomCreate,
    RoomCreateResult,
    RoomPlayerRead,
    RoomPreview,
    SelectModuleBody,
)
from app.models.content import Game, GameSystem, Scenario
from app.models.event import Event
from app.models.replay import ModuleImportJob
from app.models.room import Character, Player, Room
from app.models.user import User
from app.service import chat as chat_service
from app.service import recap as recap_service
from app.service import session_recap, table_session


class RoomNotFoundError(ValueError):
    """房间不存在。"""


class ModuleNotFoundError(ValueError):
    """模组 / 游戏 / 规则系统不存在。"""


class RoomAuthenticationError(PermissionError):
    """未提供有效的房间身份凭证。"""


class RoomAuthorizationError(PermissionError):
    """当前玩家无权执行房主操作。"""


class RoomConflictError(RuntimeError):
    """房间状态不允许当前操作（通用冲突，没有更具体的业务错误码可用时兜底）。"""


class RoomFullError(RuntimeError):
    """房间人数已满，无法加入。"""


class ModuleNotSelectedError(RuntimeError):
    """房间还没选定模组，无法开始游戏。"""


class CharacterIncompleteError(RuntimeError):
    """还有玩家未完成建卡，无法正式开局。"""


class RulesetNotConfiguredError(RuntimeError):
    """规则系统存在，但没有可用的规则数据，无法据此裁决建卡。"""


# ── 内部辅助 ──────────────────────────────────────


async def _generate_room_code(db: AsyncSession) -> str:
    """生成 6 位大写字母+数字房间码，避免碰撞。"""
    while True:
        code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
        existing = await db.scalar(select(Room).where(Room.room_code == code))
        if existing is None:
            return code


async def find_room_by_id(db: AsyncSession, room_id: str) -> Room:
    room = await db.get(Room, room_id)
    if room is None:
        raise RoomNotFoundError("房间不存在")
    return room


async def get_player_by_reconnect_token(db: AsyncSession, reconnect_token: str | None) -> Player:
    """按重连凭证查玩家——房间/角色相关接口共用的身份校验入口
    （service/character.py 也会调用这个函数）。"""
    if reconnect_token is None:
        raise RoomAuthenticationError("缺少重连凭证")
    player = await db.scalar(select(Player).where(Player.reconnect_token == reconnect_token))
    if player is None:
        raise RoomAuthenticationError("重连凭证无效")
    return player


async def _require_host(db: AsyncSession, room: Room, reconnect_token: str | None) -> Player:
    player = await get_player_by_reconnect_token(db, reconnect_token)
    if player.room_id != room.id or player.id != room.host_player_id:
        raise RoomAuthorizationError("仅房主可以执行此操作")
    return player


async def require_room_member(
    db: AsyncSession, room_id: str, reconnect_token: str | None
) -> Player:
    """校验 reconnect_token 对应的玩家确实属于这个房间——复盘/回放这类"只有
    参与者能看"的接口用。否则 roomId 会被公开房间预览暴露，任何人凭 roomId 就
    能把整局的事件日志拉走（PR #78 review 指出）。"""
    player = await get_player_by_reconnect_token(db, reconnect_token)
    if player.room_id != room_id:
        raise RoomAuthorizationError("你不是这个房间的成员")
    return player


async def _room_identity(db: AsyncSession, room: Room, player: Player) -> RoomCreateResult:
    """组装「我在这个房间里是谁」——创建/加入/重连三条路径共用。

    带上 `character_id` 是为了让**换设备重连**真正可用（PR #110 review [1]）：
    客户端靠它才知道该去拉哪张角色卡，而在此之前这个 id 只在建卡那一刻由客户端
    自己存着——换台设备就永远拿不回来了，已经建完卡的人重连后会显示成"还没建卡"、
    被引导去建第二张。服务端本来就知道答案，直接给。
    """
    character = await db.scalar(select(Character).where(Character.player_id == player.id))
    return RoomCreateResult(
        room_id=room.id,
        room_code=room.room_code,
        reconnect_token=player.reconnect_token,
        player_id=player.id,
        is_host=player.is_host,
        character_id=character.id if character is not None else None,
    )


async def _module_title(db: AsyncSession, scenario_id: str | None) -> str | None:
    if scenario_id is None:
        return None
    scenario = await db.get(Scenario, scenario_id)
    return scenario.title if scenario is not None else None


async def _to_room_preview(db: AsyncSession, room: Room) -> RoomPreview:
    # 按加入顺序排（与 `character.list_party_characters` 同一口径）。没有
    # order_by 时返回顺序由数据库决定、并不稳定——房主在列表里的位置会飘。
    # 加 AI 队友之后这个既有缺陷更容易现形：新成员可能排在房主前面。
    result = await db.scalars(
        select(Player).where(Player.room_id == room.id).order_by(Player.joined_at, Player.id)
    )
    room_players = list(result)
    return RoomPreview(
        room_id=room.id,
        room_code=room.room_code,
        room_name=room.room_name,
        phase=room.phase,
        story_started=room.phase != "Lobby",
        module_id=room.scenario_id,
        module_title=await _module_title(db, room.scenario_id),
        player_count=len(room_players),
        max_players=room.max_players,
        allow_manual_rolls=room.allow_manual_rolls,
        # 显式映射而不是 model_validate(p)：DTO 字段是 player_id，但 ORM Player
        # 的主键属性叫 id，from_attributes 按字段名找 p.player_id 会 missing。
        players=[
            RoomPlayerRead(
                player_id=p.id,
                nickname=p.nickname,
                is_host=p.is_host,
                ready=p.ready,
                has_character=p.has_character,
                is_ai=p.is_ai,
            )
            for p in room_players
        ],
    )


# ── 房间 ──────────────────────────────────────


async def create_room(db: AsyncSession, payload: RoomCreate, user: User) -> RoomCreateResult:
    """创建房间，返回房间身份信息。

    `user` 是**必需**的当前登录账号（issue #106）。此前它是可选的，于是
    `host_user_id`/`user_id` 永远是 `null`，「我的游戏」只能退而按 reconnect_token
    查、跨设备找回无从谈起。登录本来就是 2026-07-11 拍板的硬性前提，这里只是让
    实现跟上那条前提。
    """
    room_code = await _generate_room_code(db)
    now = datetime.now(UTC)

    room = Room(
        room_code=room_code,
        room_name=payload.room_name,
        max_players=payload.max_players,
        phase="Lobby",
        host_user_id=user.id,
    )
    db.add(room)
    await db.flush()  # 拿到 room.id

    player = Player(
        room_id=room.id,
        user_id=user.id,
        nickname=payload.nickname or "房主",
        is_host=True,
        joined_at=now,
    )
    db.add(player)
    await db.flush()  # 拿到 player.id

    room.host_player_id = player.id
    await db.commit()

    return await _room_identity(db, room, player)


async def join_room(
    db: AsyncSession, room_code: str, payload: JoinRoomBody, user: User
) -> RoomCreateResult:
    """用房间码加入房间；**已经是成员则幂等返回既有身份**（issue #106）。

    改动前这里有两个各自独立的缺陷：

    1. 开头一律 `if room.phase != "Lobby": raise` —— 把「中途加入」和「掉线重连」
       当成同一件事拒掉。前者确实该拒（本期不做中途加入），后者是核心能力，
       结果就是刷新/断线后必现 409、回不到自己那局。
    2. 全程不检查调用者是不是已经在房间里，无条件新建 `Player` —— 同一个人重复
       加入会生成重复玩家行、虚增人数直到撞满员。前端注释写的「已是本房间玩家
       则幂等返回已有身份」是假的。

    幂等键取**账号**而不是 `reconnect_token`：后者存在浏览器会话里，换设备、清缓存
    就没了，而「换设备也能回到这局」正是账号体系被引入的理由。两者分工不变——
    账号解决跨设备/跨时间找回，`reconnect_token` 解决同一局内的快速重连。
    """
    room = await db.scalar(select(Room).where(Room.room_code == room_code))
    if room is None:
        raise RoomNotFoundError("房间不存在")

    # 先看是不是老成员：是的话直接把既有身份还回去，不受阶段和人数上限影响
    # （重连的人本来就已经占着那个位置，拿满员去拦他没有道理）。
    existing = await db.scalar(
        select(Player).where(Player.room_id == room.id, Player.user_id == user.id)
    )
    if existing is not None:
        return await _room_identity(db, room, existing)

    # 到这里说明是新人。
    #
    # 🔴 **中途加入是允许的**（2026-08-12 放开）。聚会的物理现实是有人晚到，
    # 而在此之前他连房间都进不来——只能干等一局结束。放开之后他照常走建卡
    # （`quick_build_character` 填个名字就有一张完整的卡），入场怎么在剧情里
    # 交代由 `keeper/capabilities/presence` 负责。
    #
    # 仍然拒绝的只有**已经结束**的房间：那时没有"加入"可言，回放才是他要的。
    if room.phase == "Completed":
        raise RoomConflictError("这局已经结束了")

    count_result = await db.scalars(select(Player).where(Player.room_id == room.id))
    player_count = len(list(count_result))
    if player_count >= room.max_players:
        raise RoomFullError("房间人数已满")

    player = Player(
        room_id=room.id,
        user_id=user.id,
        nickname=payload.nickname or "玩家",
        is_host=False,
        joined_at=datetime.now(UTC),
    )
    # 上面那段「先查有没有、没有才插」是 check-then-act，两个并发的重连/加入请求
    # 会同时查到「不存在」然后各插一行（PR #110 review [2]）。真正的不变式由
    # `players` 的 `uq_players_room_user` 唯一约束保证，这里负责把撞上约束的那一方
    # **收敛成和先到者一样的结果**——毕竟两个请求想要的是同一件事。
    #
    # 🔴 必须用 SAVEPOINT（`begin_nested`）包住这次插入，不能直接 `commit()` 之后
    # 捕获再 `rollback()`：那样整个事务连同连接一起废掉，紧接着的重查要重新建连接，
    # 在异步驱动下会炸 `MissingGreenlet`——真实并发 curl 实测 10 个请求里有 2 个
    # 因此返回 500（pytest 的 ASGITransport 装置压不出并发，测不到这条）。
    # SAVEPOINT 只回滚到存档点，session 和连接都还活着，重查才做得下去。
    try:
        async with db.begin_nested():
            db.add(player)
            await db.flush()
    except IntegrityError:
        winner = await db.scalar(
            select(Player).where(Player.room_id == room.id, Player.user_id == user.id)
        )
        if winner is None:
            raise
        return await _room_identity(db, room, winner)

    await db.commit()
    return await _room_identity(db, room, player)


async def get_room_preview(db: AsyncSession, room_code: str) -> RoomPreview | None:
    """获取房间信息 + 玩家列表。"""
    room = await db.scalar(select(Room).where(Room.room_code == room_code))
    if room is None:
        return None
    return await _to_room_preview(db, room)


async def select_module(
    db: AsyncSession, room_id: str, payload: SelectModuleBody, reconnect_token: str | None
) -> None:
    """房主选定模组。"""
    room = await find_room_by_id(db, room_id)
    host = await _require_host(db, room, reconnect_token)
    if room.phase != "Lobby":
        raise RoomConflictError("只能在大厅阶段选择模组")

    scenario = await db.get(Scenario, payload.module_id)
    if scenario is None:
        raise ModuleNotFoundError("模组不存在")
    # 🔴 归属规则要落在**每一个出口**上（2026-08-18 真机顺手查出来的）。
    # `list_modules` 的说明写着「这只管谁能拿它开新局」——而开新局就是这个动作，
    # 它此前只校验了"模组存在"。于是列表里看不见别人导入的模组，拿着 id 却照样
    # 开得起来。同族判据：**一份数据有几个出口，规则就要落几处**。
    #
    # 🔴 报「模组不存在」而不是「无权使用」：后者等于替对方确认"这个 id 是有效
    # 的、只是不属于你"。同 `list_modules` 那条——连标题都不该露出去。
    if scenario.owner_user_id is not None and scenario.owner_user_id != host.user_id:
        raise ModuleNotFoundError("模组不存在")

    room.scenario_id = scenario.id
    room.system_id = scenario.game_system_id
    system = await db.get(GameSystem, scenario.game_system_id)
    room.game_id = system.game_id if system is not None else None
    room.attribute_gen_method = payload.attribute_gen_method
    await db.commit()


async def start_story(db: AsyncSession, room_id: str, reconnect_token: str | None) -> None:
    """房主在大厅点"开始游戏"，只推进到 Building（背景介绍 + 建卡）阶段。

    真正的"正式开局"（phase 变成 InGame）由 WS 的 game.start 事件触发
    （见 begin_game），必须等全员建完角色才能发生——大厅这一步只是放行玩家
    进入背景介绍和建卡流程，两者是有意分开的两个阶段。
    """
    room = await find_room_by_id(db, room_id)
    await _require_host(db, room, reconnect_token)
    if room.phase != "Lobby":
        raise RoomConflictError("只有大厅阶段可以开始游戏")
    if room.scenario_id is None:
        raise ModuleNotSelectedError("请先选择模组")
    room.phase = "Building"
    await db.commit()


async def save_turn_snapshot(db: AsyncSession, room_id: str, utterances: list[dict]) -> None:
    """存下「这一轮开始之前」的世界指针与原话，供玩家纠错回滚（`exec/35`）。

    🔴 只存指针（`keeper_state`），不存 HP/线索/骰子——那些是已经发生的事实，
    纠错不撤销它们（能撤骰子就等于能刷）。

    存不上只意味着这一轮纠不了错，不该打断对局。
    """
    room = await db.get(Room, room_id)
    if room is None:
        return
    room.last_turn_snapshot = {
        "keeper_state": room.keeper_state,
        "utterances": utterances,
    }
    await db.commit()


async def get_player(db: AsyncSession, player_id: str) -> Player | None:
    """按 player_id 直接查玩家（WS 层用客户端声明的 playerId 校验绑定用）。"""
    return await db.get(Player, player_id)


async def user_id_of_player(db: AsyncSession, player_id: str) -> str | None:
    """这名玩家背后的账号（LLM 配额算在谁头上）。

    可能是 `None`，而且那**不是异常**：AI 玩家没有账号，早期建的房间里也有
    没绑账号的历史 player 行。配额层把 `None` 当成"没人认领"并记一条 WARNING，
    见 `app/core/llm_quota.py`。
    """
    player = await db.get(Player, player_id)
    return player.user_id if player is not None else None


async def set_player_ready(db: AsyncSession, player_id: str, ready: bool) -> None:
    """WS player.ready 事件：切换大厅准备状态。"""
    player = await db.get(Player, player_id)
    if player is not None:
        player.ready = ready
        await db.commit()


async def set_player_connected(db: AsyncSession, player_id: str, connected: bool) -> None:
    """WS 连接建立/断开时维护 `Player.connected`（重连时判断用，
    本期只维护状态不接真实重连逻辑）。"""
    player = await db.get(Player, player_id)
    if player is not None:
        player.connected = connected
        if not connected:
            player.left_at = datetime.now(UTC)
        await db.commit()


_OPENING_FALLBACK = (
    "故事开始了。请在下方输入框向守秘人描述你的行动或观察——"
    "例如你看到了什么、想调查哪里、想和谁交谈。"
)


async def opening_narration_for_scenario(db: AsyncSession, scenario_id: str | None) -> str:
    """正式开局时念给玩家的开场白：优先 structured 的 opening.script，其次 player_intro。

    structured 缺失（CI/未组装）时用中性引导，避免再推「案件已加载」空壳。
    """
    if not scenario_id:
        return _OPENING_FALLBACK
    _intro, opening, pages = await _load_public_story(db, scenario_id)
    text = (opening or _intro or (pages[0] if pages else "")).strip()
    return text or _OPENING_FALLBACK


async def begin_game(db: AsyncSession, room_id: str, player_id: str) -> str:
    """WS game.start 事件：全员建完角色后，房主正式开局（Building → InGame）。

    返回 structured 开场粘贴文案，供开场仪式 LLM 失败时回退（设计 05：
    正常路径由 ws 层再跑一轮裁决→叙事开场仪式）。
    """
    room = await find_room_by_id(db, room_id)
    player = await db.get(Player, player_id)
    if player is None or player.room_id != room.id or player.id != room.host_player_id:
        raise RoomAuthorizationError("仅房主可以开始游戏")
    if room.phase != "Building":
        raise RoomConflictError("只有背景介绍/建卡阶段可以正式开局")
    result = await db.scalars(select(Player).where(Player.room_id == room.id))
    room_players = list(result)
    if not room_players or not all(p.has_character for p in room_players):
        raise CharacterIncompleteError("还有玩家未完成建卡")
    room.phase = "InGame"
    room.started_at = datetime.now(UTC)
    # 第一次聚会。往后每次续跑各开一行，「这一局聚过几次」从此是可查的数
    # （`exec/46` B3）。**同一笔事务**：场次开了而 phase 没变过去是个说不通的
    # 中间态。
    await table_session.open_session(db, room.id)
    await db.commit()
    return await opening_narration_for_scenario(db, room.scenario_id)


async def list_my_rooms(db: AsyncSession, user: User) -> list[MyRoomSummary]:
    """当前**账号**参与过的全部房间，最近活跃的排在前面（issue #106）。

    改动前这里是按 `reconnect_token` 查的，而一个重连凭证只对应一名玩家、一个
    房间——所以「我的游戏」实际上是「这个浏览器的最后一个房间」，换台设备就什么
    都看不到。账号体系当初正是为「换设备找回游戏」引入的，这里按 `user_id` 查才
    兑现了那个目的。

    ⚠️ 查询数量必须跟房间数**无关**。第一版在循环里逐个房间查人数、查模组标题，
    N 个房间要发约 `2N+2` 条查询（PR #110 review [3]）——这个接口正是本 issue 让它
    从「最多一个房间」变成「该账号全部房间」的，N 会真的长起来。下面改成先一次性
    把人数和模组标题聚合出来，再拼结果，总共 4 条查询封顶。
    """
    players = await db.scalars(select(Player).where(Player.user_id == user.id))
    room_ids = [p.room_id for p in players]
    if not room_ids:
        return []

    rooms = list(
        await db.scalars(select(Room).where(Room.id.in_(room_ids)).order_by(Room.updated_at.desc()))
    )

    # 每个房间的人数：一条 GROUP BY，不是一房一查
    count_rows = await db.execute(
        select(Player.room_id, func.count(Player.id))
        .where(Player.room_id.in_(room_ids))
        .group_by(Player.room_id)
    )
    counts = dict(count_rows.tuples().all())

    # 模组标题：把用到的 scenario_id 去重后一次查完
    scenario_ids = {room.scenario_id for room in rooms if room.scenario_id is not None}
    titles: dict[str, str] = {}
    if scenario_ids:
        title_rows = await db.execute(
            select(Scenario.id, Scenario.title).where(Scenario.id.in_(scenario_ids))
        )
        titles = dict(title_rows.tuples().all())

    return [
        MyRoomSummary(
            room_id=room.id,
            room_code=room.room_code,
            room_name=room.room_name,
            phase=room.phase,
            module_id=room.scenario_id,
            module_title=titles.get(room.scenario_id) if room.scenario_id else None,
            player_count=counts.get(room.id, 0),
            max_players=room.max_players,
            updated_at=room.updated_at,
            # 删房间是房主专属，前端得知道该不该显示那个键——让它自己拿昵称去猜
            # 是猜不出来的（房主身份在 `host_user_id` 上，列表里根本没有这一列）。
            is_host=room.host_user_id == user.id,
        )
        for room in rooms
    ]


async def end_game(db: AsyncSession, room_id: str, reconnect_token: str | None) -> None:
    """房主结束游戏，房间状态标记为已完成。

    顺带清空该房间的讨论区聊天记录（issue #107）：聊天是临时工作记忆，不进
    复盘、随房间结束销毁；`end` 是目前房间唯一的后端终结点（没有单独的
    "退出房间"接口，见 #106 本期不做），清理只能挂在这里。
    """
    room = await find_room_by_id(db, room_id)
    await _require_host(db, room, reconnect_token)
    # 🔴 散会态也能直接结束：上周打到一半收了工，这周决定不接着跑了——那时
    # 房间停在 `Adjourned`，逼房主先「继续游戏」再「结束游戏」是没道理的。
    # 加第四态时**这一处是逐个列出的地方**，只判 `!= "InGame"` 会把它锁死。
    if room.phase not in ("InGame", table_state.PHASE_ADJOURNED):
        raise RoomConflictError("只有进行中的游戏可以结束")
    await table_session.close_session(db, room.id)
    room.phase = "Completed"
    room.ended_at = datetime.now(UTC)
    await chat_service.clear_room_chat(db, room.id)
    await db.commit()


# ── 房间成员管理（踢人 / 转让房主 / 改人数 / 解散）──────────────
#
# 🔴 **踢人只在大厅阶段。** 对局中踢人要连带处理他的位置、待掷队列里挂着的
# 骰子、分组、正在等他确认的会合、已揭线索的归属——工作量是大厅版的三四倍，
# 而"开局之后想把人赶走"本来就是社交问题，不是软件问题。


async def _require_member(db: AsyncSession, room: Room, player_id: str) -> Player:
    """目标必须是这个房间的成员。跨房间操作一律拒绝——`player_id` 来自客户端。"""
    target = await db.get(Player, player_id)
    if target is None or target.room_id != room.id:
        raise RoomNotFoundError("这个玩家不在本房间")
    return target


async def kick_player(
    db: AsyncSession, room_id: str, target_player_id: str, reconnect_token: str | None
) -> None:
    """把某个人移出大厅——房主踢别人，或者**本人自己退出**。

    🔴 **自己退出也走这条**：在此之前前端的「离开房间」对非房主是纯前端导航，
    一个请求都不发——人已经走了，大厅里还挂着他的名字和一张角色卡，剩下的人
    看着以为在等他，而"全员就绪"永远凑不齐。授权口径抄 `set_player_away`
    （本人或房主），不给自己退出单开一条端点：两者要做的事一模一样，分成两条
    只会变成「加一条规则要落两处」。
    """
    room = await find_room_by_id(db, room_id)
    actor = await require_room_member(db, room.id, reconnect_token)
    if actor.id != room.host_player_id and actor.id != target_player_id:
        raise RoomAuthorizationError("只有房主能把别人移出房间")
    if room.phase != "Lobby":
        raise RoomConflictError("只有大厅阶段可以移出玩家")
    target = await _require_member(db, room, target_player_id)
    if target.id == room.host_player_id:
        # 房主要走人得先转让，否则房间会剩下一堆没有房主的人：所有需要
        # `_require_host` 的操作（选模组、开局、解散）从此全部做不了。
        raise RoomConflictError("房主不能把自己移出房间，请先转让房主")
    # 角色卡跟着人走：大厅阶段的卡还没进过对局，留着只会让下次统计人数时
    # 出现一张没有主人的卡。
    await db.execute(delete(Character).where(Character.player_id == target.id))
    await db.delete(target)
    await db.commit()


async def transfer_host(
    db: AsyncSession, room_id: str, target_player_id: str, reconnect_token: str | None
) -> None:
    """把房主交给同房间的另一个真人。

    不限阶段：真实场景恰恰是**开局之后**房主要先走（`is_host` 与
    `host_player_id` 两处都要改，否则前端看到的房主和后端认的房主会分叉）。
    """
    room = await find_room_by_id(db, room_id)
    current = await _require_host(db, room, reconnect_token)
    target = await _require_member(db, room, target_player_id)
    if target.id == current.id:
        raise RoomConflictError("你已经是房主了")
    if target.is_ai:
        # AI 拿不到 reconnect_token，也永远不会去点"开始游戏"——把房主给它
        # 等于让这个房间从此没有房主。
        raise RoomConflictError("不能把房主转让给 AI 队友")
    current.is_host = False
    target.is_host = True
    room.host_player_id = target.id
    room.host_user_id = target.user_id
    await db.commit()


async def update_room_settings(
    db: AsyncSession,
    room_id: str,
    max_players: int,
    reconnect_token: str | None,
    allow_manual_rolls: bool | None = None,
) -> None:
    """改人数上限 / 「骰子在桌上」。

    不限阶段：中途加入（放开之后）最常撞上的就是"位置不够了"，而那时候房间
    已经在 InGame。**下界是当前人数**，不是 1——调到比在座的人还少，等于让
    已经在玩的人凭空变成"超员"，而代码里没有任何地方会去踢掉多出来的人。
    """
    room = await find_room_by_id(db, room_id)
    await _require_host(db, room, reconnect_token)
    current = len(list(await db.scalars(select(Player).where(Player.room_id == room.id))))
    if max_players < current:
        raise RoomConflictError(f"房间里已经有 {current} 个人，人数上限不能小于它")
    room.max_players = max_players
    # 🔴 `None` = 不动它。这条接口原本只改人数，无条件赋值会让既有调用方
    #    （前端改人数那一处）顺手把开关重置成 False——而那不是任何人的本意。
    if allow_manual_rolls is not None:
        room.allow_manual_rolls = allow_manual_rolls
    await db.commit()


async def set_player_away(
    db: AsyncSession, room_id: str, player_id: str, away: bool, reconnect_token: str | None
) -> None:
    """中途离开 / 回来。

    **本人或房主都能按**：要走的人自己按是常态，但也有"他人已经走了、手机
    还揣兜里"的情况，那时得有人替他按。同 `exec/35` 的休息（任何人能按）——
    自用场景不需要权限模型，但这条比休息重一点（它改的是别人的角色），
    所以收窄到本人 + 房主。

    🔴 **离场要留下待交代记录**（`presence` 能力）：暂离的人下一轮就不在守秘人
    的在场名单里了，那时再想渲染"阿福离场"已经拿不到他的名字。所以在这里、
    在他还看得见的时候，把 `id@昵称` 写进 `keeper_state`。

    回来则相反：把他从待交代名单里删掉。**没交代过的离场不该在他回来之后
    还被交代一次**——那会是"阿福走了……阿福回来了"两句挤在同一段里。
    """
    room = await find_room_by_id(db, room_id)
    actor = await get_player_by_reconnect_token(db, reconnect_token)
    if actor.room_id != room.id:
        raise RoomAuthorizationError("你不是这个房间的成员")
    target = await _require_member(db, room, player_id)
    if actor.id != target.id and actor.id != room.host_player_id:
        raise RoomAuthorizationError("只能操作自己，或由房主代劳")
    if room.phase == "Completed":
        raise RoomConflictError("这局已经结束了")
    if target.away == away:
        return

    target.away = away
    state = dict(room.keeper_state or {})
    pending = presence_state.load_pending_departures(state)
    announced = presence_state.load_announced_arrivals(state)
    if away:
        pending.append((target.id, target.nickname))
    else:
        pending = [row for row in pending if row[0] != target.id]
        # 🔴 回来也要重新交代一次登场。`已交代登场` 是累积集合，不把他摘出去的话
        # 他永远算"已经介绍过"——于是他从故事里消失又出现，守秘人一个字都不会提。
        # 测试第一版是手动改 state 才过的，那正是"实现少了一步"的信号。
        announced = [pid for pid in announced if pid != target.id]
    state[presence_state.PENDING_DEPARTURES_KEY] = presence_state.serialize_departures(pending)
    state[presence_state.ANNOUNCED_ARRIVALS_KEY] = ", ".join(announced)
    room.keeper_state = state
    await db.commit()


async def get_last_session(db: AsyncSession, room_id: str) -> LastSessionRead:
    """「上次讲到哪」+ 聚过几次（`exec/46` B3）。

    🔴 **没有鉴权**：跟 `/summary` 一样。它是这一局自己的事，而且按设计
    一个字的谜底都不带（见 `session_recap._SYSTEM_PROMPT`）。
    """
    room = await find_room_by_id(db, room_id)
    return LastSessionRead(
        session_count=await table_session.session_count(db, room.id),
        recap_text=await session_recap.build_session_recap(db, room.id),
        adjourned=room.phase == table_state.PHASE_ADJOURNED,
    )


async def _require_host_player(db: AsyncSession, room: Room, player_id: str) -> Player:
    """WS 侧的房主校验：认 `player_id`，不认重连凭证。

    🔴 跟 `_require_host` 是**两套身份体系**，不是重复实现：REST 端点拿的是
    `X-Reconnect-Token`（浏览器存着的凭证），WS 连上来之后身份已经绑定成
    `bound_player_id`，手里根本没有那个 token。同 `start_game` 的做法。
    """
    player = await db.get(Player, player_id)
    if player is None or player.room_id != room.id or player.id != room.host_player_id:
        raise RoomAuthorizationError("仅房主可以做这件事")
    return player


async def adjourn_session(db: AsyncSession, room_id: str, player_id: str) -> None:
    """房主：今晚到此为止（`exec/46` B3）。

    🔴 **跟「先休息一下」是两档粒度**：休息是几分钟、任何玩家都能按、什么都
    不生成；散会是几天、只有房主能按、要留下「上次讲到哪」。两者的共同点只有
    「不开新的一轮」，那件事收在 `table_session.table_is_open`。

    🔴 **可逆**，所以门开得松：房主一人决定，不走收工那套全体确认。用户
    2026-08-24 的判断——判错的代价只是按一下继续。

    小结**不在这里生成**：那是一次 LLM 往返，会让「今晚到此为止」卡住十几秒，
    而散场那一刻大家正在收桌子。同 `recap` 的懒生成判据，第一次打开时算。
    """
    room = await find_room_by_id(db, room_id)
    await _require_host_player(db, room, player_id)
    if room.phase != "InGame":
        raise RoomConflictError("只有进行中的游戏可以收工")
    await table_session.close_session(db, room.id)
    room.phase = table_state.PHASE_ADJOURNED
    await db.commit()


async def resume_session(db: AsyncSession, room_id: str, player_id: str) -> None:
    """房主：下次聚会，接着跑。

    开一行新的场次记录，房间回到 `InGame`。**世界状态一个字都不动**——
    `keeper_state`、待掷队列、历史全都在库里躺着，续跑要做的只是把桌子重新
    打开。这也是「不做角色带到下一场」的直接后果：角色压根没离开过。
    """
    room = await find_room_by_id(db, room_id)
    await _require_host_player(db, room, player_id)
    if room.phase != table_state.PHASE_ADJOURNED:
        raise RoomConflictError("这一局没有收工，不需要继续")
    await table_session.open_session(db, room.id)
    room.phase = "InGame"
    await db.commit()


async def disband_room(db: AsyncSession, room_id: str, reconnect_token: str | None) -> None:
    """房主解散房间。

    🔴 **标记成 Completed，不删数据**：解散跟"玩完了"在数据上是同一件事——
    房间不再活着，但事件流还在，复盘/回放照常打得开。真删的话，
    `GET /rooms/{id}/replay` 会对一屋子人变成 404，而他们刚刚才玩过。

    与 `end_game` 的区别只有阶段条件：那条要求 InGame（"把这局收掉"），
    这条允许任何还没结束的阶段（"人没凑齐，散了"）。
    """
    room = await find_room_by_id(db, room_id)
    await _require_host(db, room, reconnect_token)
    if room.phase == "Completed":
        raise RoomConflictError("这个房间已经结束了")
    room.phase = "Completed"
    room.ended_at = datetime.now(UTC)
    await chat_service.clear_room_chat(db, room.id)
    await db.commit()


async def delete_room(db: AsyncSession, room_id: str, user: User) -> None:
    """房主把这个房间**彻底删掉**：房间本身 + 所有指向它的数据（玩家、角色卡、
    事件流、聊天、复盘）。

    🔴 **跟 `disband_room` 是两件事**：解散只是标成 Completed，复盘照常打得开；
    这条是"这局我不要了"（跑坏的、试手的房间该能清掉）。不可撤回，前端必须二次
    确认，并明说复盘会一起没。

    🔴 **身份走账号，不走重连凭证**：入口在「我的房间」列表，那是账号级页面，
    手上没有那个房间的 `reconnect_token`（换台设备更没有）。所以这里认
    `host_user_id`。

    🔴 **不逐个列出要清的表**：扫元数据里所有指向 `rooms.id` 的外键，按子表在前
    的顺序删。逐个列出的话，以后新加一张带 `room_id` 的表就会漏——而漏了不会有
    任何东西变红（外键在 SQLite 默认还不强制，只会静默留下孤儿行）。
    """
    room = await find_room_by_id(db, room_id)
    if room.host_user_id is None or room.host_user_id != user.id:
        raise RoomAuthorizationError("仅房主可以删除房间")

    # sorted_tables 是"被依赖的在前"，倒过来就是"子表在前"，先删引用方再删房间。
    for table in reversed(Base.metadata.sorted_tables):
        if table.name == Room.__tablename__:
            continue
        for fk in table.foreign_keys:
            if fk.column.table.name == Room.__tablename__:
                await db.execute(delete(table).where(table.c[fk.parent.name] == room_id))
    await db.execute(delete(Room).where(Room.id == room_id))
    await db.commit()


# ── 游戏 / 规则系统 / 模组目录 ──────────────────────────────


async def list_games(db: AsyncSession) -> list[GameRead]:
    """GET /api/v1/games —— 游戏大类列表。"""
    result = await db.scalars(select(Game))
    return [GameRead.model_validate(g) for g in result]


async def list_game_systems(db: AsyncSession, game_id: str) -> list[GameSystemRead]:
    """GET /api/v1/games/{gameId}/systems —— 大类下的规则系统列表。"""
    game = await db.get(Game, game_id)
    if game is None:
        raise ModuleNotFoundError("游戏大类不存在")
    result = await db.scalars(select(GameSystem).where(GameSystem.game_id == game_id))
    return [GameSystemRead.model_validate(s) for s in result]


async def get_ruleset(db: AsyncSession, system_id: str) -> RulesetRead:
    """GET /api/v1/systems/{systemId}/ruleset —— 建卡所需规则数据。

    真实数据来自 `GameSystem.ruleset`（`app/core/seed.py` seed 时用
    `app/core/coc7/content.py` 的权威数据写入）。issue #84 S1 之前这里有一份
    手写的三字符串数组兜底桩，加厚 schema 后跟 `RulesetRead` 新形状不兼容，
    且 seed 已经保证 COC7 系统一定带 ruleset，故删除——没有 ruleset 数据的
    系统（本期只有还没配置规则数据的自定义系统会出现这种情况）直接返回空
    目录，而不是伪造一份看起来正常但内容不对的数据。
    """
    system = await db.get(GameSystem, system_id)
    if system is None:
        raise ModuleNotFoundError("规则系统不存在")
    if system.ruleset:
        return RulesetRead.model_validate(system.ruleset)
    return RulesetRead(attributes=[], skills=[], occupations=[])


async def require_ruleset(db: AsyncSession, system_id: str) -> RulesetRead:
    """裁决路径专用的取数：拿不到可用规则数据就直接拒绝，不返回空目录。

    跟 `get_ruleset` 的区别是**用途**，不是数据源——两者读的是同一张表：

    - `get_ruleset` 服务于 `GET /systems/{id}/ruleset`（前端渲染用）。规则数据
      为空时返回空目录是合理的：前端拿到空目录就知道这个系统还没配规则。
    - `require_ruleset` 服务于**裁决**（建卡 `complete` 校验、`preview` 计算）。
      规则计算改参数注入后（issue #112），属性键/技能表/职业目录全部来自传入的
      `RulesetRead`，空目录会让校验退化成"零个约束"——空白角色卡一条问题都查不出
      来，`complete_character` 会把它标记成完成。校验闸门不能 fail-open，所以这里
      宁可报错也不放行。

    参数注入之前这条路径是安全的，因为属性键写死在 `coc7_rules` 的模块常量里，
    与规则数据是否存在无关；把数据源变成参数之后，"没有数据"就成了一种必须显式
    处理的输入。
    """
    ruleset = await get_ruleset(db, system_id)
    if not ruleset.attributes or not ruleset.occupations:
        raise RulesetNotConfiguredError("该规则系统尚未配置规则数据，无法建卡")
    return ruleset


async def list_modules(db: AsyncSession, *, user_id: str | None = None) -> list[ModuleRead]:
    """获取可用模组列表：内置的（无主）+ 我自己导入的。

    🔴 **必须按主人过滤。** 在导入功能落地之前这张表里只有内置模组，所以
    "返回全部"是对的；导入落地的那一刻它就变成"每个人都能看到别人导入的模组"
    ——连第三方模组的标题都露出去了。同族判据：**放开一个约束前，先找谁在依赖
    它**（这里依赖的是"scenarios 表里只有无主行"）。

    `user_id=None`（没登录）只看得到内置模组。**不是看到全部**——未登录退化成
    更大的可见范围是最坏的一种默认值。

    注意这只管"谁能拿它开新局"。已经在玩的房间照旧看 `rooms.scenario_id`，
    同房间其他玩家不需要拥有这个模组（`Scenario.owner_user_id` 的注释）。
    """
    stmt = select(Scenario).where(Scenario.owner_user_id.is_(None))
    if user_id is not None:
        stmt = select(Scenario).where(
            or_(Scenario.owner_user_id.is_(None), Scenario.owner_user_id == user_id)
        )
    result = await db.scalars(stmt)
    return [_to_module_read(s) for s in result]


def _to_module_read(scenario: Scenario) -> ModuleRead:
    """`is_imported` 由 `owner_user_id` 推出，不另存一份状态。"""
    dto = ModuleRead.model_validate(scenario)
    dto.is_imported = scenario.owner_user_id is not None
    dto.created_at = scenario.created_at
    return dto


async def _load_public_story(
    db: AsyncSession, module_id: str
) -> tuple[str | None, str | None, list[str]]:
    """从 structured 读玩家可见前情；解析不出/损坏时返回空。

    内置走文件、导入走库，两者的区别收在 `contract/source.py` 里（`exec/29`）。
    """
    from pathlib import Path

    from app.core.config import get_settings
    from app.core.keeper.contract.catalog import default_modules_dir
    from app.core.keeper.contract.module_loader import public_story_from_module
    from app.core.keeper.contract.source import resolve_module

    settings = get_settings()
    modules_dir = (
        Path(settings.keeper_modules_dir).expanduser().resolve()
        if settings.keeper_modules_dir
        else default_modules_dir()
    )
    try:
        resolved = await resolve_module(db, modules_dir, module_id)
    except Exception:
        # JSON 坏 / schema 不符时不拖垮详情接口；列表仍有 synopsis
        return None, None, []
    if resolved is None:
        return None, None, []
    return public_story_from_module(resolved.module)


async def delete_module(db: AsyncSession, module_id: str, user_id: str) -> None:
    """DELETE /api/v1/modules/{moduleId} —— 把自己导入的模组从库里删掉。

    ## 🔴 为什么需要它

    在这之前模组**只进不出**：导错了、导重了、导坏了，全部永久堆在「我的模组」
    和建房下拉列表里。而**导入的自查闭环本来就是断的**（剧透约束让导入者看不到
    切成什么样，只能开一局才知道好不好），于是"重导一份"是常规操作——没有删除
    就等于每试一次就永久脏一格。

    ## 门：有房间在用就拒绝

    用户 2026-08-19 拍板选了最保守那档。理由是判据「**悬空的指针比没有指针更坏**」：
    `rooms.scenario_id` 没有 `ondelete`，删了之后那些房间的世界会凭空消失，而
    复盘/回放还指着它。

    拒绝时**报出有几个房间在用**——「加一道门，必须同时给它配一条走得通的修法」：
    解散那些房间就能删了，而房主本来就有解散能力。

    ## 只有主人能删，且看不到的当不存在

    比 `_may_read_module` 严格一档：那里"同房间的人"也能读（否则别人的前情页
    会空白），但删除只认主人。内置模组 `owner_user_id is None`，同一个判断顺带
    挡住——**内置的是随发版进来的目录，不该被任何账号删掉**。

    看不到就抛 404 而不是 403，跟 `get_module_detail` 同口径：不确认"这个 id
    存在但你没权限"。

    ## 清理范围

    八张 `scenario_id` 外键表**一张都不能漏**（它们没有一个配了 `ondelete`）
    ——所以这里**扫外键、不逐个列出**：新加一张挂 `scenario_id` 的表，它会自动
    被清到，而手写清单会漏（「逐个列出的地方，加一项就漏一项」）。

    最后把指向它的导入任务的 `result_scenario_id` 清空：那是**引用方**，
    判据「删引用方之前先清指针」——留着的话导入记录页会拿一个死 id 去开局。
    """
    scenario = await db.get(Scenario, module_id)
    if scenario is None or scenario.owner_user_id != user_id:
        raise AppException(ErrorCode.NOT_FOUND, "模组不存在", status.HTTP_404_NOT_FOUND)

    in_use = await db.scalar(select(func.count(Room.id)).where(Room.scenario_id == module_id))
    if in_use:
        raise AppException(
            ErrorCode.CONFLICT,
            f"还有 {in_use} 个房间在用这份模组，先解散它们再删。",
            status.HTTP_409_CONFLICT,
        )

    # 引用方的指针先清空，别删：导入任务是历史记录，`retried_from_job_id` 这条
    # 链要留着（"用户点三次要知道前两次为什么失败"）。只把死指针摘掉。
    await db.execute(
        update(ModuleImportJob)
        .where(ModuleImportJob.result_scenario_id == module_id)
        .values(result_scenario_id=None)
    )

    # 🔴 扫外键、按子表在前的顺序删——形状与 `disband_room` 完全一致（那里的
    # 理由同样成立：逐个列出的话，以后新加一张带 `scenario_id` 的表就会漏，
    # 而漏了不会有任何东西变红，外键在 SQLite 默认还不强制）。
    #
    # `rooms` 排除在外：走到这里说明没有房间在用它（上面那道门），而 rooms 的
    # `scenario_id` 可空、房间本身也不该被删模组这个动作带走。
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in {Scenario.__tablename__, Room.__tablename__}:
            continue
        for fk in table.foreign_keys:
            if fk.column.table.name == Scenario.__tablename__:
                await db.execute(delete(table).where(table.c[fk.parent.name] == module_id))

    await db.execute(delete(Scenario).where(Scenario.id == module_id))
    await db.commit()


async def _may_read_module(db: AsyncSession, scenario: Scenario, user_id: str) -> bool:
    """这个人能不能看这份模组的玩家前情。

    三种可以（**其余一律不能**）：

    1. **内置模组**（`owner_user_id is None`）——它是目录，登录了就看得见；
    2. **自己导入的**；
    3. **正在某个用了这份模组的房间里**——这一条不能省：三个调用点全在房间内
       （`StoryPage` 的前情页、`RoomPage` 的 `playerIntro` 与旧占位句替换），
       而**同房间的其他玩家并不拥有这个模组**（`Scenario.owner_user_id` 的注释
       写着"已经在玩的房间照旧看 `rooms.scenario_id`"）。只按主人过滤的话，
       导入模组的房间里除房主外所有人的前情页当场变空白。
    """
    if scenario.owner_user_id is None or scenario.owner_user_id == user_id:
        return True
    seat = await db.scalar(
        select(Player.id)
        .join(Room, Room.id == Player.room_id)
        .where(Player.user_id == user_id, Room.scenario_id == scenario.id)
        .limit(1)
    )
    return seat is not None


async def get_module_detail(
    db: AsyncSession, module_id: str, user_id: str
) -> ModuleDetailRead | None:
    """GET /api/v1/modules/{moduleId} —— 模组详情（含 structured 玩家前情）。

    🔴 **这个端点此前没有任何鉴权**（2026-08-19 补）：它返回的确实只有玩家可见
    的那部分（`player_intro` / `opening_script` / `story_pages`，绝不含 `kp_truth`），
    但**导入模组是有主的**——知道 id 的任何人（此前连登录都不需要）都能读到别人
    导入的模组前情。`list_modules` 早就按主人过滤了，详情这一头漏了。
    同族判据：**一份数据有几个出口，规则就要落几处。**

    看不到的一律当**不存在**（不是 403），跟 `5cfbe6c` 给「拿它开新局」那个出口
    定的口径一致——不确认"这个 id 存在但你没权限"。
    """
    scenario = await db.get(Scenario, module_id)
    if scenario is None:
        return None
    if not await _may_read_module(db, scenario, user_id):
        return None
    detail = ModuleDetailRead.model_validate(scenario)
    intro, opening, pages = await _load_public_story(db, module_id)
    if not pages and detail.synopsis:
        pages = [detail.synopsis]
    return detail.model_copy(
        update={
            "player_intro": intro,
            "opening_script": opening,
            "story_pages": pages,
        }
    )


# ── 复盘 / 事件回放 ──────────────────────────────────────


async def record_event(
    db: AsyncSession,
    room_id: str,
    player_id: str | None,
    event_type: str,
    payload: dict,
    *,
    event_id: str | None = None,
) -> str:
    """写入一条房间事件（issue #77 才真正打通的闭环——原来"不记 EventLog"是
    已知缺口，本期由 ws.py 在 narration.push / action.submit 时调用这个函数）。

    返回事件 id：广播 payload 要带上它，前端才能拿事件身份去重（exec/19 #42）。

    `event_id`：**流式叙事要先有 id 再开始推**（`exec/28`）——第一条
    `narration.delta` 就得带上它，前端才知道后续碎片该拼到哪条消息上。所以
    那条路径先自己生成 id，写库时再把同一个 id 传回来。不传就照旧自动生成。
    """
    event = Event(room_id=room_id, player_id=player_id, event_type=event_type, payload=payload)
    if event_id is not None:
        event.id = event_id
    db.add(event)
    await db.commit()
    return event.id


# 叙事上下文里带多少条行动历史。取值权衡：太少 AI 上文接不住，太多白白烧
# token——单轮生成（非编排）的定位下 6 条足够撑起"延续刚才的场景"。
_NARRATION_HISTORY_LIMIT = 6


async def build_narration_context(
    db: AsyncSession, room_id: str, player_id: str, utterance: str
) -> NarrationContext:
    """为一次 action.submit 组装叙事生成的上下文（issue #107）。

    数据来源只有两处：房间关联的模组标题 + `events` 表里最近几条
    `action.submit`（**不读聊天表**——讨论区内容永远不进 LLM 上下文，这是
    #107 跟 AI 编排对齐的第 1 条约定，靠这里的代码结构保证）。

    ⚠️ 调用时序约定：ws.py 必须在 `record_event` 写入当前这条 action.submit
    **之前**调用本函数——这样查出来的历史天然不含当前这条（它会作为"玩家刚
    说的话"单独出现在 prompt 末尾，不该在历史里重复）。靠时序排除比靠
    "player_id+内容匹配"过滤可靠：玩家完全可能重复说过一模一样的话。

    Narrator（app/core/narration/）自己不查库，所有字段由这里备好传入。
    """
    player = await db.get(Player, player_id)
    nickname = player.nickname if player is not None else "玩家"

    room = await db.get(Room, room_id)
    module_title = await _module_title(db, room.scenario_id) if room is not None else None

    # 最近 N 条行动：按倒序取再反转成时间正序喂给模型。
    result = await db.execute(
        select(Event)
        .where(Event.room_id == room_id, Event.event_type == "action.submit")
        .order_by(Event.created_at.desc(), Event.id.desc())
        .limit(_NARRATION_HISTORY_LIMIT)
    )
    history = list(result.scalars())
    history.reverse()

    # 批量查昵称，不在循环里逐条查（N+1 的教训，PR #110 review [3]）
    speaker_ids = {e.player_id for e in history if e.player_id is not None}
    nicknames: dict[str, str] = {}
    if speaker_ids:
        rows = await db.execute(
            select(Player.id, Player.nickname).where(Player.id.in_(speaker_ids))
        )
        # Row 是 tuple 的子类但类型上不是 tuple[str, str]，直接喂 dict() 过不了
        # 类型检查——用 .tuples() 显式转成类型化元组再构造。
        nicknames = dict(rows.tuples().all())

    recent_actions = [
        f"{nicknames.get(e.player_id or '', '玩家')}: {e.payload.get('utterance', '')}"
        for e in history
    ]
    return NarrationContext(
        utterance=utterance,
        player_nickname=nickname,
        module_title=module_title,
        recent_actions=recent_actions,
        room_id=room_id,
        player_id=player_id,
    )


#: 🔴 **绝不能出现在玩家复盘里的事件类型**（exec/25 #61）。
#:
#: `get_replay` 把 `payload` **原样**返回给玩家。`keeper.decision` 里装的是
#: 裁决的审计信息：`thinking` 在 prompt 里就写明"玩家看不到"，`player_state`
#: 会直接暴露守秘人对这句话的判断。它落表是为了让**我们**能诊断，不是为了
#: 讲故事——复盘讲的是"发生了什么"，不是"守秘人当时怎么想的"。
#:
#: 这是黑名单，不是白名单——改成白名单要动现有全部事件类型，风险不成比例。
#: 代价是：**新增任何带敏感 payload 的事件类型时，必须回来加进这个集合**。
#: 纯审计事件：落库是为了让**我们**能诊断，不该出现在玩家的复盘时间线里。
#:
#: 🔴 这是一张**逐个列出**的表（下面 `get_replay` 的注释里点了它的名）：
#: 加一类审计事件就得回来加一行，漏了不会有任何东西变红。2026-08-18 加
#: `keeper.progress` 时当场漏过一次——那是每拍一条的记账留痕，玩家看到
#: `{"advanced": false}` 毫无意义。
_REPLAY_HIDDEN_EVENT_TYPES = frozenset({"keeper.decision", "keeper.progress"})


async def get_replay(
    db: AsyncSession, room_id: str, reconnect_token: str | None
) -> list[ReplayEventRead]:
    """GET /api/v1/rooms/{roomId}/replay —— 逐条事件回放，按发生时间正序。

    先校验发起者是这个房间的成员（复盘是"只有参与者能看"的内容），再查事件。
    审计类事件按 `_REPLAY_HIDDEN_EVENT_TYPES` 排除。

    ## 🔴 对局中按受众裁，散场之后全开（exec/33 §10 #78 的另一半）

    分头叙事每段落库时都写了 `payload.audience`（当初为审计留的），**却一直
    没有消费方**——而前端进房/刷新正是靠这个接口重建时间线，于是刷新一次就把
    另一组的叙事全拿到了。这跟待掷卡片补发是同一个病：**同一件事的两头，
    一头做了一头没做**；只是这一头宽得多（整局的分段叙事，不只是一张卡）。

    `phase == "Completed"` 之后不再裁：分头的保密前提是「你不在场」，散场之后
    大家本来就会互相讲，而复盘的价值恰恰是看见别人那半边（用户 2026-08-11 裁定）。

    判据是**声明式**的：只裁"自己声明了受众"的那些行，没声明的照旧。加一类
    带受众的事件时，它自动被裁——不必回来改这里（对比上面那个黑名单）。
    """
    player = await require_room_member(db, room_id, reconnect_token)
    room = await find_room_by_id(db, room_id)
    result = await db.scalars(
        select(Event)
        .where(
            Event.room_id == room_id,
            Event.event_type.not_in(_REPLAY_HIDDEN_EVENT_TYPES),
        )
        .order_by(Event.created_at)
    )
    events = list(result)
    if room.phase != "Completed":
        events = [e for e in events if _replay_visible_to(e, player.id)]
    return [ReplayEventRead.model_validate(e) for e in events]


def _replay_visible_to(event: Event, player_id: str) -> bool:
    """这一行事件该不该出现在这个人的回放里。

    没有 `audience` 字段 = 没有声明受众 = 公开，照旧可见（P5.2 之前的全部
    事件、以及所有未分头的轮次都走这一支，行为逐字不变）。
    """
    audience = (event.payload or {}).get("audience")
    if audience is None:
        return True
    return player_id in audience


async def get_summary(db: AsyncSession, room_id: str) -> RoomSummaryRead:
    """GET /api/v1/rooms/{roomId}/summary —— 复盘摘要。

    实现在 `service/recap.py`：上半是代码算的数字，下半是模型写的一段回顾。
    这里只做转发，是因为复盘要读整条事件流、还要打一次网络，跟房间的 CRUD
    不是一类事。
    """
    try:
        return await recap_service.build_summary(db, room_id)
    except LookupError as exc:
        raise RoomNotFoundError(str(exc)) from exc
