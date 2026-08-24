"""顶层 `/ws/{roomId}` WebSocket 路由（issue #60，issue #77 补齐 14 个新事件 +
切换为真实 ORM 读写）。

故意不挂在 `/api/v1` 前缀下——前端约定的连接地址是
`ws://host/ws/{roomId}?token={token}`，是独立于 REST API 版本号的实时通道，
`roomId` 是房间内部 ID（不是玩家分享用的 roomCode）。

协议（跟 trpg-app 原型 services/api-client.ts 对齐）：
- 客户端发送 `{type, playerId, payload}`；
- 服务端推送 `{type, payload}`；
- 连接后第一条消息必须是 `room.join`，成功后回 `session.bound`，
  在此之前收到的其它事件类型会被忽略（还没确认这个连接对应哪个玩家）；
- `player.ready`/`game.start`/`action.submit` 读写 `players`/`rooms` 表，
  玩家列表/准备/建卡完成/阶段仍然靠前端轮询 `GET /rooms/{roomCode}` 获取
  （issue #77"三处原型取舍"表格，`room.state`/`player.joined` 协议槽位已经
  留好，但本期不会真的发出）。
- `action.submit` 的叙事回复本期是固定文案的占位实现（"Mock 叙事"，
  issue #43 允许），真实 AI 叙事生成留给 #43 落地。
- 断线重连**没有单独的事件**：客户端重连时照常发 `room.join`，服务端用
  `reconnect_token` 认人，`session.bound` 之后补发待掷卡片与会合确认卡。
  （曾经铺过一条 `room.rejoin` 协议，从未接通，2026-08-13 删除。）
  `check.roll`/`san.check.roll`（两段式玩家掷骰，feat/keeper-agent）：确认
  并结算守秘人已发起的待掷检定，服务端权威生成骰值——keeper 模式下是真实
  实现，非 keeper 模式（Fallback/DeepSeekNarrator 没有"待掷检定"的概念）
  回 `NOT_IMPLEMENTED`。
- 每条广播出去的 `narration.push` 都会同步写一行 `events` 表——这是本期
  唯一真正打通的事件日志闭环，`GET /rooms/{roomId}/replay` 直接读它。

数据库会话按"每条消息一个短 session"处理，而不是整条连接复用一个：一个
WebSocket 可能存活很久，用一个 session 包住整条连接会在这期间一直占着一个
数据库连接/事务，跟并发的 HTTP 请求争抢 SQLite 的锁（测试里表现为死锁）。
鉴权单独用一个短 session，之后每条消息各开各的，消息之间等待时不持有连接。
"""

import asyncio
import contextlib
from dataclasses import replace
from uuid import uuid4

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory
from app.core.errors import ErrorCode
from app.core.llm_quota import QuotaExceeded, quota_subject
from app.core.narration.contract import (
    CheckRequestNotice,
    CheckResultNotice,
    NarrationDeltaSink,
    NarrationOutcome,
    NarrationSegment,
    PlayerUtterance,
    SegmentDeltaSinkFactory,
    StatChangeNotice,
)
from app.core.table_state import PHASE_ADJOURNED, table_is_open
from app.dto.ws import (
    ActionBroadcastPayload,
    ActionSubmitPayload,
    CharacterStatChangedPayload,
    ChatMessagePayload,
    ChatSendPayload,
    CheckRequestPayload,
    CheckResultPayload,
    CheckRollPayload,
    ClientEnvelope,
    EndGameDecidePayload,
    EndGameRequestPayload,
    EndGameStatusPayload,
    ErrorPayload,
    GameStartPayload,
    KeeperBusyPayload,
    KeeperPhasePayload,
    LuckDecidePayload,
    LuckOfferPayload,
    NarrationDeltaPayload,
    NarrationPushPayload,
    PartyMergeConfirmPayload,
    PartyUpdatePayload,
    PlayerReadyPayload,
    RoomAdjournedPayload,
    RoomAdjournPayload,
    RoomJoinPayload,
    RoomPausedPayload,
    RoomPausePayload,
    SanCheckRequestPayload,
    SanCheckResultPayload,
    SanCheckRollPayload,
    ServerEnvelope,
    SessionBoundPayload,
    TurnClarifyPayload,
)
from app.service import auth as auth_service
from app.service import chat as chat_service
from app.service import room as room_service
from app.service import session_recap, table_session
from app.service.action_lock import action_lock_manager
from app.service.turn_window import Submission, merge_utterances, turn_window_manager
from app.service.ws_manager import manager

router = APIRouter()
logger = structlog.get_logger()

_UNAUTHORIZED_CLOSE_CODE = 4401
_NOT_FOUND_CLOSE_CODE = 4404


async def _send_turn_failure(websocket: WebSocket, exc: Exception) -> None:
    """一轮跑砸了，告诉玩家。**额度用完和服务器出错不是一回事。**

    🔴 三处宽捕获（action.submit / check.roll / 幸运消费）原本都直接发
    `INTERNAL_ERROR「请稍后重试」`。配额闸门加上之后，那句话对"今天额度用完了"
    是**误导**——玩家会照着提示一直重试，而每次重试都再记一次账，越试越深。
    「加一道门，必须同时给它配一条走得通的修法」：这里就是那条修法。

    收成一个函数而不是在三处各写一遍 if：这是"逐个列出的地方"，加第四处时
    照抄的人会自动抄到这个区分。`test_ws_quota.py` 钉住了三处都走它。
    """
    if isinstance(exc, QuotaExceeded):
        await _send_error(websocket, ErrorCode.RATE_LIMITED.value, exc.message)
        return
    await _send_error(websocket, "INTERNAL_ERROR", "守秘人暂时无法回应，请稍后重试")


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    """只发给触发这次交互的那一个连接，不广播——`error` 事件是"告诉发起者
    这次请求怎么了"，不是房间广播内容（issue #77 新增）。"""
    payload = ErrorPayload(code=code, message=message)
    envelope = ServerEnvelope(type="error", payload=payload.model_dump(by_alias=True))
    await websocket.send_json(envelope.model_dump(by_alias=True))


#: 「点一下就该生效」的小操作等房间锁最多等这么久（秒）。一轮叙事实测 13–18s。
_SMALL_OP_LOCK_WAIT_SECONDS = 30.0


async def _acquire_for_small_op(room_id: str) -> str | None:
    """等房间锁，等不到才放弃。给**不需要 LLM 的小状态修改**用。

    🔴 为什么不能像 `action.submit` 那样当场拒绝（2026-08-10 多人验证跑）：
    那两条路上被拒的是**玩家已经点下去的按钮**——「已会合」和「投掷」。锁在
    整轮叙事期间（10 秒级）一直握着，于是真机上出现了两件事：确认会合点了
    没反应；护栏刚说「请先完成待掷的检定」，玩家点掷骰又被同一把锁挡回去。
    **一句话的提示 + 一个必须再点一次的按钮**，玩家只会认为坏了。

    发言可以回「已记下」（话在空气里，KP 稍后处理），按钮不行——它没有缓冲区。
    所以这里改成**等**：反正这两件事本来就该排在当前这一轮后面。
    """
    deadline = asyncio.get_running_loop().time() + _SMALL_OP_LOCK_WAIT_SECONDS
    while True:
        token = action_lock_manager.try_acquire(room_id)
        if token is not None:
            return token
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(0.2)


async def _broadcast_narration(
    db: AsyncSession,
    room_id: str,
    player_id: str | None,
    text: str,
    *,
    event_id: str | None = None,
) -> None:
    """广播一条 narration.push，并同步写一行 `events` 表——`GET
    /rooms/{roomId}/replay` 读的就是这里写入的数据（issue #77 才打通的
    EventLog 闭环，此前"不记 EventLog"是已知缺口）。
    """
    # 🔴 先落库再广播：广播 payload 要带上事件 id，前端按它去重（exec/19 #42）。
    # 顺序反过来就拿不到 id，只能退回"按正文文本去重"——那正是 #42 的病根。
    #
    # 流式路径是例外，且**不是对上面那条的违反**：它在开流之前就自己生成了
    # id（`exec/28`），此时把同一个 id 传回来落库。身份仍然先于广播存在，
    # 只是生成它的地方从数据库挪到了调用方。
    event_id = await room_service.record_event(
        db, room_id, player_id, "narration.push", {"text": text}, event_id=event_id
    )
    narration = NarrationPushPayload(text=text, event_id=event_id)
    envelope = ServerEnvelope(type="narration.push", payload=narration.model_dump(by_alias=True))
    await manager.broadcast(room_id, envelope.model_dump(by_alias=True))


def _narration_delta_sink(room_id: str, event_id: str) -> NarrationDeltaSink:
    """给 keeper 用的 delta 投递口（`exec/28`）。

    🔴 **delta 不落库。** `events` 表仍然只在整段写完时落一行完整叙事，
    `GET /rooms/{roomId}/replay` 一行不用改。重连的人拿到的是那一行完整文本，
    **不重放流式**——刷新页面后把整局叙事重新打一遍字，玩家会疯
    （`exec/26 #62` 的第一条要求）。

    delta 是加速通道，不是事实来源；两者靠同一个 `eventId` 认亲。
    """

    async def _sink(seq: int, text: str) -> None:
        payload = NarrationDeltaPayload(event_id=event_id, seq=seq, text=text)
        envelope = ServerEnvelope(type="narration.delta", payload=payload.model_dump(by_alias=True))
        await manager.broadcast(room_id, envelope.model_dump(by_alias=True))

    return _sink


def _segment_delta_sink_factory(room_id: str) -> SegmentDeltaSinkFactory:
    """分头叙事的 delta 投递口（`exec/33 §3.2`）：**按段裁受众**。

    🔴 这条跟 §4 的并行是**绑定的**，不许单独做任何一半。此前分头路径不产
    delta，全房间广播那个 sink 因此"碰巧不漏"；并行化的第一步就是让两段同时
    往外写字——那一刻如果还用 `manager.broadcast`，**并行落地那天就是泄露
    落地那天**，而且不会有任何东西变红。
    受众由 agent 按段传进来（它才知道这一段是写给谁的），这里只负责投递。
    """

    def _factory(event_id: str, audience: tuple[str, ...]) -> NarrationDeltaSink:
        async def _sink(seq: int, text: str) -> None:
            payload = NarrationDeltaPayload(event_id=event_id, seq=seq, text=text)
            envelope = ServerEnvelope(
                type="narration.delta", payload=payload.model_dump(by_alias=True)
            )
            await manager.send_to_players(
                room_id, list(audience), envelope.model_dump(by_alias=True)
            )

        return _sink

    return _factory


async def _deliver_narration_segments(
    db: AsyncSession, room_id: str, segments: list[NarrationSegment]
) -> None:
    """分头探索（P5.2）：各处各看各的，只发给该地点在场的连接。

    事件照样落库、且**每段一行**——守秘人永远看得见全部（私密是玩家↔玩家，
    不是玩家↔KP，exec/18），历史重放与复盘都要完整。`audience` 写进 payload
    供审计"这段当时发给了谁"。
    """
    for segment in segments:
        if not segment.text:
            continue
        # 流式的段落在开流之前就有 id（delta 靠它认亲），这里复用同一个；
        # 非流式的传 None，由 record_event 分配——与 §3.2 之前逐字一致。
        event_id = await room_service.record_event(
            db,
            room_id,
            None,
            "narration.push",
            {
                "text": segment.text,
                "audience": list(segment.audience),
                "nodeId": segment.node_id,
            },
            event_id=segment.event_id,
        )
        payload = NarrationPushPayload(text=segment.text, private=segment.covert, event_id=event_id)
        envelope = ServerEnvelope(type="narration.push", payload=payload.model_dump(by_alias=True))
        await manager.send_to_players(
            room_id, list(segment.audience), envelope.model_dump(by_alias=True)
        )


async def _broadcast_keeper_busy(room_id: str, busy: bool) -> None:
    """守秘人开始/结束这一轮（`exec/33 §5.4`）。

    全房间广播是**对的**：这是元层信息（"KP 在忙"），不含任何虚构内容。
    前端的「守秘人正在思考」是本地在自己提交时点亮的，**没发言的人看不到**，
    分头时另一组因此整整十几秒黑屏。
    """
    payload = KeeperBusyPayload(busy=busy)
    envelope = ServerEnvelope(type="keeper.busy", payload=payload.model_dump(by_alias=True))
    await manager.broadcast(room_id, envelope.model_dump(by_alias=True))


async def _fresh_room(db: AsyncSession, room_id: str):
    """读**这一拍执行完之后**的房间状态。

    🔴 **ws.py 里不许直接 `db.get(Room, ...)`**，一律走这里（`test_architecture`
    有 AST 守护钉着）。

    ## 为什么（2026-08-18 真机，两局都撞到）

    一条 WS 消息的形状是这样的：

        async with async_session_factory() as db:      # 这条消息自己的 session
            room = await room_service.find_room_by_id(db, room_id)   # ① 进 identity map
            await _ingest_utterance(db, ...)           # ② record_event 里 commit 了
            await _run_turn(db, ...)                   # ③ narrator 用**自己的**
                                                       #    session 写 keeper_state 并提交
            await _push_after_turn(db, ...)            # ④ 又在**同一个** db 上读

    `db.get()` 先查 identity map，命中就**不发 SQL**；而 `core/db.py` 有意设了
    `expire_on_commit=False`（②那次 commit 因此不会让对象失效）。于是 ④ 读到的
    是 ① 那一刻的快照 —— **整整落后一拍**。

    实测后果三样，一样比一样重：
      - `party.update` 的位置落后一拍（下一拍会自己纠正）；
      - `keeper.phase` 落后一拍，**而这一条不会自己纠正**：`finished` 那一拍
        后面没有下一拍了。第二局库里已经是 `finished`/`truth`，整局 20 条
        `keeper.phase` 一条 `finished` 都没有，玩家屏幕永远停在「调查中」；
      - `_audience_at_speaker_location` 拿它算**投递受众与隐匿名单** ⇒ 这一拍
        刚移动过的人，检定卡按**移动前**的同处关系投递（分头局的隔离泄漏）。

    🔴 **重连之所以是好的，恰恰因为它是新 session** —— 08-16 修「重连不补发
    `keeper.phase`」时那一半是对的，但它把这第二个根因盖住了两天。

    `populate_existing=True` 就够：②那次 commit 已经把事务关掉了，重新 SELECT
    拿得到新值（三种候选修法都在真实形状下验过，见 `test_ws_reads_fresh_state`）。
    """
    from app.models.room import Room

    return await db.get(Room, room_id, populate_existing=True)


async def _push_keeper_phase(
    db: AsyncSession,
    websocket: WebSocket,
    room_id: str,
    *,
    only_player_id: str | None = None,
) -> None:
    """把「这一局走到哪一步了」推给玩家（2026-08-15）。

    🔴 **补的是一条只有一半的链**：`closure` 早就会把 phase 写成 `ending` /
    `finished`，叙事纪律与字数上限也跟着变，**而前端一个字都收不到**。实测里
    玩家连说三次「结束了吧」，界面毫无变化——他没法知道这一局到底结没结束。
    """
    from app.core.keeper.runtime.phase import load_ending_id, load_phase

    room = await _fresh_room(db, room_id)
    if room is None:
        return
    payload = KeeperPhasePayload(
        phase=load_phase(room.keeper_state) or "", ending_id=load_ending_id(room.keeper_state)
    )
    envelope = ServerEnvelope(type="keeper.phase", payload=payload.model_dump(by_alias=True))
    if only_player_id is not None:
        await websocket.send_json(envelope.model_dump(by_alias=True))
        return
    await manager.broadcast(room_id, envelope.model_dump(by_alias=True))


async def _push_after_turn(
    db: AsyncSession,
    websocket: WebSocket,
    room_id: str,
    *,
    only_player_id: str | None = None,
) -> None:
    """一回合结束（或重连补发）之后**要推给玩家的每一样东西**。

    🔴 存在的理由是那条判据：**逐个列出的地方，加一项就漏一项**。位置这一条
    此前散在四个调用点上，再加一条"对局阶段"就是四处各加一行，第五条必漏。
    收成一个函数之后，加一样只改这里。
    """
    await _push_party_update(db, websocket, room_id, only_player_id=only_player_id)
    await _push_keeper_phase(db, websocket, room_id, only_player_id=only_player_id)
    await _push_character_may_have_changed(websocket, room_id, only_player_id=only_player_id)


async def _push_character_may_have_changed(
    websocket: WebSocket,
    room_id: str,
    *,
    only_player_id: str | None = None,
) -> None:
    """「现在是重读角色卡的安全点」。不查库——它不声称任何东西真的变了。

    理由见 `CharacterMayHaveChangedPayload`：改角色卡的路径散在好几片能力里，
    而前端原来是逐个事件配一条重拉，幸运和装备两样都漏了。
    """
    envelope = ServerEnvelope(type="character.may_have_changed", payload={})
    if only_player_id is not None:
        await websocket.send_json(envelope.model_dump(by_alias=True))
        return
    await manager.broadcast(room_id, envelope.model_dump(by_alias=True))


async def _push_party_update(
    db: AsyncSession,
    websocket: WebSocket,
    room_id: str,
    *,
    only_player_id: str | None = None,
) -> None:
    """把每个人**自己的**空间处境推给他（`exec/33 §5.4`）。

    🔴 逐人裁过再发：别处那一组在哪、有谁，对你的角色不该知道（他们可能正在
    潜行）。所以只给「我在哪 · 谁跟我在一处 · 另有几组人在别处」。
    这只眼睛存在的理由是——系统把位置认错时，此前**没有任何人会发现**。

    `only_player_id`：**重连补发**用（`exec/34`）。这时只给刚回来的那条连接发
    他自己那一组——别人手上的状态好好的，广播一遍会插进他们的消息序列里
    （`test_reconnect_sends_nothing_extra_when_the_queue_is_empty` 当场抓到）。
    判据同 `_resend_pending_checks` 那句"只发给这一条刚绑定的连接，不广播"。
    """
    from sqlalchemy import select

    from app.core.keeper.runtime.location_state import group_players
    from app.core.keeper.runtime.pending import MERGE_CONFIRM_KIND, pending_decision_manager
    from app.models.room import Player

    room = await _fresh_room(db, room_id)
    if room is None:
        return
    keeper_state = room.keeper_state
    rows = await db.execute(select(Player.id, Player.nickname).where(Player.room_id == room_id))
    roster: list[tuple[str, str]] = [(pid, nick) for pid, nick in rows.all()]
    if not roster:
        return

    # 名字由**持有剧本的那一层**解析：ws 层不持有剧本（它在 narrator 里按房间
    # 加载），也不该自己缓存一份——那就是第二份真相。拿不到就显示 id，
    # **不编造名字**。
    narrator = getattr(websocket.app.state, "narrator", None)
    resolver = getattr(narrator, "location_label", None)

    async def _label(location_id: str | None) -> str | None:
        if location_id is None:
            return None
        if resolver is None:
            return location_id
        return await resolver(room_id, keeper_state, location_id)

    pending = await pending_decision_manager.player_ids_of_kind(db, room_id, MERGE_CONFIRM_KIND)
    groups = group_players(keeper_state, [pid for pid, _ in roster], pending)
    nicknames: dict[str, str] = dict(roster)
    for location_id, members in groups:
        if only_player_id is not None and only_player_id not in members:
            continue
        # 待确认的人自己一组（group_players 保证），所以那一组的位置就是"他站在
        # 哪里等确认"。**不从待确认记录里读位置**——那份拷贝已经删了，位置只有一份。
        merge_at = location_id if any(pid in pending for pid in members) else None
        payload = PartyUpdatePayload(
            location_id=location_id,
            location_name=await _label(location_id),
            companions=[nicknames.get(pid, pid) for pid in members],
            other_groups=len(groups) - 1,
            merge_pending_at=await _label(merge_at),
        )
        envelope = ServerEnvelope(type="party.update", payload=payload.model_dump(by_alias=True))
        await manager.send_to_players(room_id, list(members), envelope.model_dump(by_alias=True))


async def _handle_merge_confirm(
    db: AsyncSession, websocket: WebSocket, room_id: str, player_id: str
) -> None:
    """当事人确认「我确实跟他们碰上了」（`exec/33 §5.2`）。

    只有确认这一个动作，**没有否认**：不确认就是维持分离，那本来就是默认与
    安全方向；也**没有超时自动确认**——超时自动 = 静默泄露。
    """
    from app.core.keeper.runtime.deps import KeeperToolError
    from app.core.keeper.runtime.location_state import confirm_merge_impl

    # keeper_state 是整列读-改-写，必须跟裁决那条循环串行（同 check.roll 的理由）。
    lock_token = await _acquire_for_small_op(room_id)
    if lock_token is None:
        await _send_error(websocket, "ACTION_IN_PROGRESS", "守秘人正在处理其他玩家的行动，请稍候")
        return
    async with action_lock_manager.held(room_id, lock_token):
        try:
            changed = await confirm_merge_impl(db, room_id, player_id)
        except KeeperToolError as exc:
            await _send_error(websocket, "MERGE_CONFIRM_FAILED", str(exc))
            return
    if changed:
        await _push_after_turn(db, websocket, room_id)


async def _handle_end_game_decide(
    db: AsyncSession,
    websocket: WebSocket,
    room_id: str,
    player_id: str,
    payload: EndGameDecidePayload,
) -> None:
    """玩家对「收工吗」表态（2026-08-19）。

    全票 → 落 `finished` 并把 `keeper.phase` 推给所有人；一票拒绝 → 清空整批，
    继续玩。两种结果都发一条 `game.end.status`，否则表完态的人只看到卡片消失、
    不知道发生了什么。
    """
    from app.core.keeper.runtime.deps import KeeperToolError
    from app.core.keeper.runtime.end_game import decide_end_game

    # 跟裁决循环串行：它改 keeper_state（阶段），同 check.roll 的理由。
    lock_token = await _acquire_for_small_op(room_id)
    if lock_token is None:
        await _send_error(websocket, "ACTION_IN_PROGRESS", "守秘人正在处理其他玩家的行动，请稍候")
        return
    async with action_lock_manager.held(room_id, lock_token):
        try:
            outcome = await decide_end_game(db, room_id, player_id, accepted=payload.accepted)
        except KeeperToolError as exc:
            await _send_error(websocket, "END_GAME_DECIDE_FAILED", str(exc))
            return
        await db.commit()

    await manager.broadcast(
        room_id,
        ServerEnvelope(
            type="game.end.status",
            payload=EndGameStatusPayload(
                waiting_for=list(outcome.waiting_for),
                declined_by=outcome.declined_by,
                finished=outcome.finished,
            ).model_dump(by_alias=True),
        ).model_dump(by_alias=True),
    )
    if outcome.finished:
        # 阶段变了就得推——这条链此前断过一次（2026-08-16「收尾在玩家侧不存在」），
        # 而这里是**玩家自己按下的结束**，收不到反馈比任何时候都刺眼。
        await _push_keeper_phase(db, websocket, room_id)


async def _audience_at_speaker_location(
    db: AsyncSession, room_id: str, player_id: str, *, private: bool = False
) -> list[str] | None:
    """这句原话该发给谁；**返回 None = 照旧全房间广播**。

    三条规则，按优先级：
    1. 玩家自己勾了私密（exec/18 ⑥）或正处于隐匿状态（②）→ 只回给他本人；
    2. 全队已分头（P5.2 ①）→ 只发给同处一地的人。不然"你不在场所以你不知道"
       只挡住了守秘人的叙事，队友在地窖喊的那句照样出现在你屏幕上；
    3. 都不是 → None，走原来的全房间广播，行为逐字不变。

    找不到发言者时返回 `[player_id]`（只发给他自己）而不是 None：这条路径
    上的错误必须**朝保密的方向**失败，退化成广播就是当场泄密。
    """
    from sqlalchemy import select

    from app.core.keeper.runtime.location_state import group_players, load_hidden_players
    from app.core.keeper.runtime.pending import MERGE_CONFIRM_KIND, pending_decision_manager
    from app.models.room import Player

    room = await _fresh_room(db, room_id)
    keeper_state = room.keeper_state if room is not None else None
    if private or player_id in load_hidden_players(keeper_state):
        return [player_id]
    # AI 玩家算进分组（exec/21 第一层）。**分组用全量玩家、发送用连接**：
    # 下面 `send_to_players` 按 player_id 找连接，AI 没有连接自然发不到，
    # 那是对的——算上它是为了让"谁跟谁在一处"算对，不是为了给它发字节。
    rows = await db.execute(select(Player.id).where(Player.room_id == room_id))
    groups = group_players(
        keeper_state,
        list(rows.scalars()),
        await pending_decision_manager.player_ids_of_kind(db, room_id, MERGE_CONFIRM_KIND),
    )
    if len(groups) <= 1:
        return None
    for _node_id, members in groups:
        if player_id in members:
            return members
    return [player_id]


_OPENING_CEREMONY_UTTERANCE = (
    "（开场仪式：模组没有现成开场脚本。请用简短段落建立场景与处境，"
    "让调查员知道身在何处；不要灌水，不要发起检定。）"
)


async def _run_opening_ceremony(
    db: AsyncSession,
    websocket: WebSocket,
    room_id: str,
    player_id: str,
    fallback_text: str,
) -> str:
    """设计 05：game.start 后给出**唯一**开场旁白，并初始化 opening 阶段。

    全模组统一（禁止双重引导）：
    1. narrate 生成一段——有 structured 开场脚本时，`agent.py` 会把它当
       **素材**喂给叙事阶段的 LLM 改写，不再原样粘贴（真人实测 09-#2：原样
       粘贴等于让 AI 主持人"照本宣科"念模组书的背景说明，模组数据里混进的
       GM 指导语等缺陷也会被原样带进游戏，见神秘渡轮的真实案例）。
    2. LLM 失败/空结果（含 Fallback 无 key 场景）→ structured 脚本原样兜底。
    3. 都没有 → 中性兜底。
    """
    from app.core.narration.contract import NarrationContext

    script = (fallback_text or "").strip()
    player = await room_service.get_player(db, player_id)
    nickname = player.nickname if player is not None else "玩家"
    used_llm = False
    text = ""

    narrator = websocket.app.state.narrator
    context = NarrationContext(
        utterance=_OPENING_CEREMONY_UTTERANCE,
        player_nickname=nickname,
        module_title=None,
        recent_actions=[],
        room_id=room_id,
        player_id=player_id,
        is_heartbeat=False,
        is_opening_ceremony=True,
    )
    try:
        outcome = await narrator.narrate(context)
        text = (outcome.text or "").strip()
    except Exception as exc:  # 开场失败不能卡死进局
        logger.warning("opening_ceremony_failed", room_id=room_id, error=str(exc))
        text = ""

    # 权威顺序：narrate 结果 > structured 脚本 > 中性兜底（全模组只推一段）
    # 🔴 认常量不认字面量（`exec/23 #76` 改文案时撞见）：写死那半句的话，文案
    # 一改它就**静默停止匹配**，开场那条路会把守卫提示当成真叙事推出去。
    # 顺带补上漏掉的那一种——原来只认掷骰那条，幸运卡那条从来没在这里出现过。
    from app.core.keeper.runtime.agent import LUCK_PENDING_NOTICE, ROLL_PENDING_NOTICE

    if text and text not in (ROLL_PENDING_NOTICE, LUCK_PENDING_NOTICE):
        used_llm = True
    elif script:
        text = script
        used_llm = False
    else:
        text = await room_service.opening_narration_for_scenario(db, None)
        used_llm = False

    try:
        from app.core.keeper.runtime.heartbeat import touch_activity

        touch_activity(room_id)
    except Exception:  # noqa: BLE001
        pass
    await room_service.record_event(
        db,
        room_id,
        player_id,
        "keeper.opening_ceremony",
        {"source": "game.start", "used_llm": used_llm, "single_opening": True},
    )
    return text


async def _current_san_of(db: AsyncSession | None, player_id: str) -> int | None:
    """这名玩家此刻的理智值。拿不到就 None——**卡片少一个数字不该打断一次检定**。

    读法跟结算那半**共用 `deps.current_stat`**（`san_check/executor.py` 用的
    同一个），不另写「从 attributes 取 POW」之类的近似——那就是「一份判据落
    两处」，迟早分叉。`current_stat` 读的是 `derived_stats` 里的当前值，
    建卡写进去的上限被备份成 `SAN_MAX`，所以这里拿到的是掉过之后的真实值。
    """
    if db is None:
        return None
    # 函数内 import，跟本文件里 `KeeperToolError` 等几处同一个理由：
    # ws 层不在模块顶层依赖 keeper 内部实现。
    from sqlalchemy import select

    from app.core.keeper.runtime.deps import KeeperToolError, current_stat
    from app.models.room import Character

    # 「一个玩家在一个房间只有一张卡」已经由唯一约束保证（迁移 e2b91f4c7a56），
    # 所以这里不需要 ORDER BY 去挑"哪张算数"。
    # 🔴 `populate_existing`：同 `_fresh_room` 的理由 —— 一条消息里这个函数会被
    # 调用多次（一拍两个检定就够了），而中间那次结算会在**另一个** session 里改
    # SAN。不加的话第二次拿到的是第一次那个旧对象。
    character = await db.scalar(
        select(Character)
        .where(Character.player_id == player_id)
        .execution_options(populate_existing=True)
    )
    if character is None:
        return None
    try:
        return current_stat(character, "SAN")
    except KeeperToolError:
        # 角色卡没有 SAN 数据（建卡未完成等）。**不伪造**一个数字。
        return None


def _check_request_envelope(
    notice: CheckRequestNotice,
    *,
    current_san: int | None,
) -> tuple[SanCheckRequestPayload | CheckRequestPayload, str]:
    """待掷通知 → (payload, 事件类型)。首发广播与重连补发共用同一份组装。

    🔴 `current_san` 是**必需的关键字参数**，不给默认值。此前这里硬写着
    `current_san=None`，于是整条链都在（DTO 有字段、推送发得出、SDK 有类型、
    前端在读），**只有值从来没填过**——2026-08-17 真机才发现玩家看到的理智值
    永远是空。给它一个默认值就是同一个坑再挖一遍：漏传是静默的，
    而必需参数漏传当场红。
    """
    if notice.kind == "san":
        return (
            SanCheckRequestPayload(
                player_id=notice.player_id,
                current_san=current_san,
                check_request_id=notice.check_request_id,
                reason=notice.reason or None,
            ),
            "san.check.request",
        )
    return (
        CheckRequestPayload(
            player_id=notice.player_id,
            skill=notice.skill or "",
            # 🔴 2026-08-16：这里此前写死 `None` 且没有注释，于是掷骰卡片上
            # 只有技能名——玩家掷之前不知道自己要过多少。字段一直在契约里、
            # `check.result` 那边也一直在填，**只有请求这一侧没人写**。
            target_value=notice.target,
            check_request_id=notice.check_request_id,
            reason=notice.reason or None,
        ),
        "check.request",
    )


async def _broadcast_check_request(
    room_id: str, notice: CheckRequestNotice, db: AsyncSession | None = None
) -> None:
    """推一次"待掷检定"通知（两段式玩家掷骰）：玩家看到卡片、点击后才生成骰值。

    🔴 **按位置裁受众**（2026-08-10 多人实测）：此前是全房间广播，实测中在客厅
    的阿贵收到了阿福在地下室的「侦察」卡片——同一条投递隔离，叙事那一半早就
    按位置裁了，检定这一半漏了。**同一件事的两头，一头做了一头没做。**
    （服务端拒绝替掷，所以这是泄露与噪声，不是越权掷骰。）

    `db=None` 时退化成全房间广播——只留给拿不到会话的调用点，现在没有。
    """
    payload, event_type = _check_request_envelope(
        notice, current_san=await _current_san_of(db, notice.player_id)
    )
    envelope = ServerEnvelope(type=event_type, payload=payload.model_dump(by_alias=True))
    await _send_to_colocated(db, room_id, notice.player_id, envelope.model_dump(by_alias=True))


async def _send_to_colocated(
    db: AsyncSession | None, room_id: str, player_id: str, envelope: dict
) -> None:
    """发给跟这个人同处一地的连接；未分头（或拿不到会话）时照旧全房间。"""
    audience = (
        await _audience_at_speaker_location(db, room_id, player_id) if db is not None else None
    )
    if audience is None:
        await manager.broadcast(room_id, envelope)
    else:
        await manager.send_to_players(room_id, audience, envelope)


async def _broadcast_check_result(
    room_id: str, notice: CheckResultNotice, db: AsyncSession | None = None
) -> None:
    """推一次检定结果（玩家点击掷骰确认后，服务端权威生成的骰值）。

    受众同 `_broadcast_check_request`：别处的人不该看见这边掷了多少。
    """
    if notice.kind == "san":
        payload: SanCheckResultPayload | CheckResultPayload = SanCheckResultPayload(
            player_id=notice.player_id,
            roll_value=notice.rolled,
            san_loss=notice.san_loss or 0,
            result=notice.level,
            check_request_id=notice.check_request_id,
            san_remaining=notice.san_remaining,
        )
        event_type = "san.check.result"
    else:
        payload = CheckResultPayload(
            player_id=notice.player_id,
            skill=notice.skill or "",
            roll_value=notice.rolled,
            target_value=notice.target,
            result=notice.level,
            check_request_id=notice.check_request_id,
            opposed_opponent=notice.opposed_opponent,
            opposed_roll_value=notice.opposed_rolled,
            opposed_target_value=notice.opposed_target,
            opposed_result=notice.opposed_level,
            opposed_won=notice.opposed_won,
            opposed_verdict=notice.opposed_verdict,
            effective_roll_value=notice.effective_rolled,
            luck_spent=notice.luck_spent,
        )
        event_type = "check.result"
    envelope = ServerEnvelope(type=event_type, payload=payload.model_dump(by_alias=True))
    await _send_to_colocated(db, room_id, notice.player_id, envelope.model_dump(by_alias=True))


async def _broadcast_stat_change(
    room_id: str, notice: StatChangeNotice, db: AsyncSession | None = None
) -> None:
    """推一次 HP 变更（真人实测 09-#4 修复）：裁决判定伤害后立即执行，没有
    对应的检定/掷骰事件可以携带新值，此前只拼进叙事正文当纯文本，前端角色卡
    拿不到结构化数据。这条只推 HP——San 已经有 `san.check.result` 携带
    `san_remaining`，走"检定→掷骰→广播结果"这条路，不需要这个事件。

    🔴 **受众同检定结果，不是全房间**（`exec/33 §3.1`）：payload 里带着
    `reason`（「被壁橱里的东西抓伤」这类），全房间推等于告诉另一组
    **这边有人受伤了、还是被什么伤的**——分头刚做成结构性的，这是最后一个
    绕过位置的通道。掉血是虚构世界里发生的事，按位置裁；元层（谁在等谁、
    KP 在忙）才可以全房间。
    """
    payload = CharacterStatChangedPayload(
        player_id=notice.player_id, hp=notice.hp, hp_max=notice.hp_max, reason=notice.reason or None
    )
    envelope = ServerEnvelope(
        type="character.stat_changed", payload=payload.model_dump(by_alias=True)
    )
    await _send_to_colocated(db, room_id, notice.player_id, envelope.model_dump(by_alias=True))


async def _handle_room_join(
    db: AsyncSession,
    websocket: WebSocket,
    room_id: str,
    player_id: str | None,
    reconnect_token: str,
) -> bool:
    """处理 room.join：校验 playerId 属于这个房间、且出示了该玩家的
    reconnect_token（证明是本人，不是拿别人 playerId 冒充），成功后登记连接并回
    session.bound。返回是否绑定成功。
    """
    player = await room_service.get_player(db, player_id) if player_id else None
    if player is None or player.room_id != room_id or player.reconnect_token != reconnect_token:
        await websocket.close(code=_NOT_FOUND_CLOSE_CODE)
        return False
    assert player_id is not None  # 上面能走到这里，player_id 必然非空（见 get_player 调用）
    # 连接登记必须带上玩家身份：per-observer 投递（P5.2）要能回答"这条连接是谁"。
    from app.core.keeper.runtime.pending import MERGE_CONFIRM_KIND, pending_decision_manager

    manager.add(room_id, websocket, player_id)
    await room_service.set_player_connected(db, player_id, True)
    payload = SessionBoundPayload(room_id=room_id, player_id=player_id)
    envelope = ServerEnvelope(type="session.bound", payload=payload.model_dump(by_alias=True))
    await websocket.send_json(envelope.model_dump(by_alias=True))
    await _resend_pending_checks(db, websocket, room_id, player_id)
    # 🔴 会合确认卡也要补发（exec/34 §2.1）：它跟待掷检定是同一件事——"等某个
    # 玩家做决定"——却因为当初各写了一套，只有掷骰那半有重连补发。症状是断线
    # 重连之后「已会合」按钮消失，而服务端还记着他挂在那儿，两组就一直分着。
    #
    # 🔴 **只在他真的挂着卡时才发，且只发给这一条连接**：握手在没有任何待决定
    # 项时必须**逐字不变**（`test_reconnect_sends_nothing_extra_when_the_queue_is_empty`
    # 守着这条，我加第一版时当场被它抓到）。同 `_resend_pending_checks` 的规矩。
    if player_id in await pending_decision_manager.player_ids_of_kind(
        db, room_id, MERGE_CONFIRM_KIND
    ):
        await _push_after_turn(db, websocket, room_id, only_player_id=player_id)
    await _resend_keeper_phase(db, websocket, room_id, player_id)
    return True


async def _resend_keeper_phase(
    db: AsyncSession, websocket: WebSocket, room_id: str, player_id: str
) -> None:
    """重连补发「这一局走到哪一步了」。

    🔴 **2026-08-16 真机：这一局真的结束了，而玩家永远看不到。** `keeper.phase`
    只在阶段变化那一刻广播过一次；重连握手一条都不补，前端 `keeperPhase` 停在
    初值空串，「本局结束」那条横幅的条件 `keeperPhase === 'finished'` 永远不成立。
    触发条件是刷新页面 / 掉线 / 换设备——**一旦发生就再也看不到收尾**，而收尾
    正是这两轮的主验收项。

    🔴 **这是我上一批亲手挖的坑**：把 `keeper.phase` 挂进 `_push_after_turn`
    时没数它有几个出口，而重连路径上的那次调用被关在「这个玩家挂着会合确认卡」
    这个**跟阶段毫无关系**的条件里。「这一局结不结束」和「有没有待决定项」
    共用了一个开关。

    补发的判据不是"哪几种阶段要发"（那又是逐个列出），而是：**客户端自己
    重建不出来、而服务端有值**——所以 phase 写过就发。没写过（大厅、还没开局）
    一条都不发，`test_reconnect_sends_nothing_extra_when_the_queue_is_empty`
    守的那条"握手逐字不变"因此仍然成立。
    """
    from app.core.keeper.runtime.phase import load_phase

    room = await _fresh_room(db, room_id)
    if room is None or load_phase(room.keeper_state) is None:
        return
    await _push_keeper_phase(db, websocket, room_id, only_player_id=player_id)


async def _resend_pending_checks(
    db: AsyncSession, websocket: WebSocket, room_id: str, viewer_id: str
) -> None:
    """重连后补发还没掷的检定卡片（真人实测 exec/23 #56）。

    `check.request` 只在裁决那一刻**实时推过一次**。刷新页面或断线重连的人
    再也收不到它——而队列里那条检定还在，`narrate` 的守卫会一直挡着新一轮，
    于是对局停在"守秘人等你掷骰、你屏幕上却没有骰子"的死角。

    队列落库（exec/24 §8.1）只解决了"服务端不会忘"，这一半解决"客户端能想起来"
    ——两个都做了，重启才真的能接着玩。

    只发给**这一条刚绑定的连接**，不广播：别人手上的卡片好好的，重发一遍只会
    在他们屏幕上多出一张重复卡。

    🔴 **要按受众裁**（exec/33 §10 #78，双人真机一局复现两次）：首发是裁过的
    （`_broadcast_check_request` → `_send_to_colocated`），补发这一半却把房间里
    **所有**卡片原样推给刚绑定的连接——阿福在屋后掷潜行，阿贵刷新一下页面就
    连那张卡带理由文本一起收到了。又是**同一件事的两头，一头做了一头没做**。
    判据只能有一份：这里复用首发那个 `_audience_at_speaker_location`，
    不另写一套。
    """
    from app.core.keeper.runtime.pending import (
        END_GAME_KIND,
        LUCK_SPEND_KIND,
        ROLL_KINDS,
        pending_decision_manager,
        to_notice,
    )

    async def _may_see(owner_id: str) -> bool:
        audience = await _audience_at_speaker_location(db, room_id, owner_id)
        return audience is None or viewer_id in audience

    for pending in await pending_decision_manager.list_all(db, room_id, ROLL_KINDS):
        if not await _may_see(pending.player_id):
            continue
        notice = to_notice(pending)
        payload, event_type = _check_request_envelope(
            notice, current_san=await _current_san_of(db, notice.player_id)
        )
        envelope = ServerEnvelope(type=event_type, payload=payload.model_dump(by_alias=True))
        await websocket.send_json(envelope.model_dump(by_alias=True))
    # 幸运卡同理——它同样只在骰子停下那一刻推过一次，而且**它挂着的时候整轮
    # 停在那儿**（`TURN_BLOCKING_KINDS`）。漏了这一半就是 `exec/23 #56` 第三次
    # 复发：服务端记得、客户端不知道。
    for offer in await pending_decision_manager.list_all(db, room_id, {LUCK_SPEND_KIND}):
        if not await _may_see(offer.player_id):
            continue
        envelope = ServerEnvelope(
            type="luck.offer", payload=_luck_offer_payload(offer).model_dump(by_alias=True)
        )
        await websocket.send_json(envelope.model_dump(by_alias=True))
    # 收工确认卡同理。🔴 **它不按受众裁**（同首发）：「这一局要不要结束」是整桌
    # 的事，刷新一下页面就再也看不到自己那张票，等于把一票否决权吞掉了。
    # 「持久化必须配套重连补发」这条已经复发过三次，加 kind 时一并配上。
    for card in await pending_decision_manager.list_all(db, room_id, {END_GAME_KIND}):
        envelope = ServerEnvelope(
            type="game.end.request", payload=_end_game_payload(card).model_dump(by_alias=True)
        )
        await websocket.send_json(envelope.model_dump(by_alias=True))


# `offer` 是 PendingDecision——这一层不 import 它（keeper 运行时不该被控制器拖进来）。
def _luck_offer_payload(offer) -> LuckOfferPayload:  # noqa: ANN001
    """幸运卡 → 推送 payload。首发与重连补发共用（同 `_check_request_envelope`）。"""
    notice = offer.payload["notice"]
    return LuckOfferPayload(
        decision_id=offer.decision_id,
        player_id=offer.player_id,
        skill=notice["skill"] or "",
        rolled=notice["rolled"],
        target=notice["target"],
        cost=offer.cost,
        luck_remaining=offer.luck_remaining,
        opposed_opponent=notice.get("opposed_opponent"),
    )


async def _broadcast_luck_offer(  # noqa: ANN001
    room_id: str, offer, db: AsyncSession | None = None
) -> None:
    """推一张幸运卡。受众同 `check.request`——别处的人不该看见这边在掷什么。

    同处一地的队友也收得到（前端只给本人渲染按钮）：他们至少知道**桌子为什么
    停在这儿**，这跟 `keeper.busy` 是同一个存在感问题（`exec/33 §5.4`）。
    """
    envelope = ServerEnvelope(
        type="luck.offer", payload=_luck_offer_payload(offer).model_dump(by_alias=True)
    )
    await _send_to_colocated(db, room_id, offer.player_id, envelope.model_dump(by_alias=True))


def _end_game_payload(offer) -> EndGameRequestPayload:  # noqa: ANN001
    """收工确认卡 → 推送 payload。首发与重连补发共用。"""
    return EndGameRequestPayload(
        decision_id=offer.decision_id,
        player_id=offer.player_id,
        initiator=str(offer.payload.get("initiator") or ""),
    )


async def _broadcast_end_game_request(  # noqa: ANN001
    room_id: str, offer, db: AsyncSession | None = None
) -> None:
    """推一张「收工吗」确认卡。

    🔴 **全房间广播，不按位置裁**——跟检定卡正相反。分头两处的人也要收到：
    「这一局要不要结束」是**整桌的事**，按位置裁会让另一处的人既看不到提议、
    也无从表态，而他们的票是必需的（全票才结束）。
    """
    envelope = ServerEnvelope(
        type="game.end.request", payload=_end_game_payload(offer).model_dump(by_alias=True)
    )
    await manager.broadcast(room_id, envelope.model_dump(by_alias=True))


async def _broadcast_player_offer(  # noqa: ANN001
    room_id: str, offer, db: AsyncSession | None = None
) -> None:
    """把一张待决定卡推出去，**按 kind 分发**。

    🔴 这里此前写死了 `_broadcast_luck_offer`：`player_offers` 是「等某个玩家
    答一句」的统称，不是幸运卡专用，而写死之后加第二种 kind 就会静默丢失
    （没有消费方 = 没加）。分发表放一处，加 kind 时只改这里。
    """
    from app.core.keeper.runtime.pending import END_GAME_KIND, LUCK_SPEND_KIND

    if offer.kind == END_GAME_KIND:
        await _broadcast_end_game_request(room_id, offer, db)
    elif offer.kind == LUCK_SPEND_KIND:
        await _broadcast_luck_offer(room_id, offer, db)
    else:  # pragma: no cover - 新 kind 忘了登记时要看得见，不静默吞掉
        logger.warning("ws_unknown_player_offer_kind", room_id=room_id, kind=offer.kind)


async def _handle_chat_send(
    db: AsyncSession,
    websocket: WebSocket,
    room_id: str,
    player_id: str,
    payload: ChatSendPayload,
) -> None:
    """处理 chat.send：落库（重发幂等）后把 chat.message 广播给全房间。

    讨论区消息**不写 events 表、不进任何 LLM 上下文**——它跟 action.submit
    是两条独立通道（两个界面），这是 issue #107 的立项设计。
    """
    text = payload.text.strip()
    if not text:
        return
    player = await room_service.get_player(db, player_id)
    if player is None or player.room_id != room_id:
        return
    # 游戏结束后禁止写入讨论消息——否则 /end 清理后仍存活的 WS 可以重新落库，
    # 导致清理失效且无法再次调用 /end 清除。
    try:
        room = await room_service.find_room_by_id(db, room_id)
        if room.phase == "Completed":
            await _send_error(websocket, "FORBIDDEN", "游戏已结束，无法发送消息")
            return
    except room_service.RoomNotFoundError:
        return
    message = await chat_service.save_chat_message(
        db, room_id, player_id, text, payload.client_message_id
    )
    chat_message = ChatMessagePayload(
        message_id=message.id,
        player_id=message.player_id,
        nickname=player.nickname,
        text=message.text,
        sent_at=message.created_at,
        client_message_id=message.client_message_id,
    )
    envelope = ServerEnvelope(
        type="chat.message", payload=chat_message.model_dump(by_alias=True, mode="json")
    )
    await manager.broadcast(room_id, envelope.model_dump(by_alias=True))


async def _ingest_utterance(
    db: AsyncSession,
    room_id: str,
    player_id: str,
    nickname: str,
    utterance: str,
    *,
    private: bool = False,
) -> bool:
    """把一句原话记进本轮：落库 → 按受众广播 → 并入收集窗口。返回"是否开了新的一轮"。

    真人和 AI 队友（exec/21 第三层）走的是**同一个函数**。AI 不该有任何特权
    路径——它说的话一样落 events 表、一样按位置裁受众、一样并进同一个收集
    窗口，于是历史重放、per-audience 裁剪、复盘对它天然成立。
    """
    # 原话先广播：不管这条是开窗的还是并入的，同处一地的人都该**立刻**看见。
    # 分头后只发给在场的那几个（P5.2）；未分头时 audience 是 None，走原来的
    # 全房间广播，行为逐字不变。
    audience = await _audience_at_speaker_location(db, room_id, player_id, private=private)
    # 🔴 受众随事件一起落库：历史重放要能回答"这句话当时谁听见了"。没有它，
    # P5.2d 的 per-audience 上下文裁剪就无从判断历史行的可见性——事后再猜位置
    # 是猜不回来的。不写这个键 = 公开。
    # 落库在广播**之前**：广播 payload 要带这一行的 id，前端按事件身份去重
    # （exec/19 #42：此前只能按原话文本去重，同一句话说第二次就被永久吞掉）。
    event_payload: dict = {"utterance": utterance, "private": private}
    if audience is not None:
        event_payload["audience"] = audience
    event_id = await room_service.record_event(
        db, room_id, player_id, "action.submit", event_payload
    )
    action_envelope = ServerEnvelope(
        type="action.broadcast",
        payload=ActionBroadcastPayload(
            player_id=player_id, nickname=nickname, utterance=utterance, event_id=event_id
        ).model_dump(by_alias=True),
    ).model_dump(by_alias=True)
    if audience is None:
        await manager.broadcast(room_id, action_envelope)
    else:
        await manager.send_to_players(room_id, audience, action_envelope)

    return turn_window_manager.join(
        room_id,
        Submission(player_id=player_id, nickname=nickname, utterance=utterance, private=private),
    )


async def _handle_clarify(
    db: AsyncSession,
    websocket: WebSocket,
    room_id: str,
    player_id: str,
    clarification: str | None,
) -> None:
    """玩家纠错（`exec/35`）：「你把我的话理解错了」→ 回滚指针 → 重裁上一轮。

    `clarification=None` = **重试**（`turn.retry`）：同样回滚、同样重跑上一轮，
    只是不多给模型那句话。守秘人调用失败之后玩家需要的正是它。

    ## 为什么需要它

    真人 KP 桌上最高频的交互就是纠错。我们此前一次都没做——玩家唯一的手段是
    再说一句话，指望模型自己发现。真机实测多次出现位置被写错（`exec/31 #72`、
    `exec/33 #79`），每一次都只能靠改代码修，**玩家在桌上没有出路**。

    ## 边界：回滚指针，不回滚事实

    - **回滚**：`keeper_state`（谁在哪、当前场景、分组、常驻状态）—— 一次赋值。
    - **回滚**：还没掷的待决定项。它们是**基于误解**发起的，留着既没意义又会
      挡住新一轮（待掷守卫）。
    - **不回滚**：HP/SAN 变化、已揭开的线索、**掷出的骰子**。它们是既成事实；
      骰子尤其不能撤——能撤就等于能刷。

    ## 谁能按

    任何人。被误解的不一定是发言的人——真机那次是「阿福说话、阿贵被挪走」。
    """
    # 函数内 import：keeper 那半边反过来依赖 controller 会成环（同本文件
    # 其余几处待决定队列的用法）。
    from app.core.keeper.runtime.pending import pending_decision_manager

    room = await room_service.find_room_by_id(db, room_id)
    snapshot = (room.last_turn_snapshot or {}) if room is not None else {}
    saved_utterances = snapshot.get("utterances") or []
    if not saved_utterances:
        await _send_error(websocket, "CONFLICT", "现在没有可以纠正的回合")
        return

    # 🔴 等锁，不当场拒绝：这是**按钮**，没有缓冲区（同 `_acquire_for_small_op`
    # 的理由）。而且纠错最可能被按下的时刻，正是上一轮刚讲完、锁刚放开那一拍。
    lock_token = await _acquire_for_small_op(room_id)
    if lock_token is None:
        await _send_error(websocket, "CONFLICT", "守秘人正忙，稍后再试")
        return
    async with action_lock_manager.held(room_id, lock_token):
        # 澄清本身是玩家说的话：落库 + 按受众广播，跟别的发言一条路
        # （历史、replay、per-audience 裁剪因此天然成立）。
        # ⚠️ 不并入收集窗口——它不开新的一轮，它重跑旧的那一轮。
        # 重试（clarification=None）没有这句话，跳过整段。
        if clarification:
            player = await room_service.get_player(db, player_id)
            nickname = player.nickname if player is not None else "调查员"
            audience = await _audience_at_speaker_location(db, room_id, player_id)
            event_payload: dict = {"utterance": clarification, "clarification": True}
            if audience is not None:
                event_payload["audience"] = audience
            event_id = await room_service.record_event(
                db, room_id, player_id, "action.submit", event_payload
            )
            envelope = ServerEnvelope(
                type="action.broadcast",
                payload=ActionBroadcastPayload(
                    player_id=player_id,
                    nickname=nickname,
                    utterance=clarification,
                    event_id=event_id,
                ).model_dump(by_alias=True),
            ).model_dump(by_alias=True)
            if audience is None:
                await manager.broadcast(room_id, envelope)
            else:
                await manager.send_to_players(room_id, audience, envelope)

        # 回滚指针 + 清掉基于误解发起的待决定项
        if room is not None:
            room.keeper_state = snapshot.get("keeper_state")
            await pending_decision_manager.clear_room(db, room_id)
            await db.commit()

        submissions = [
            Submission(
                player_id=str(u.get("player_id") or ""),
                nickname=str(u.get("nickname") or "调查员"),
                utterance=str(u.get("text") or ""),
            )
            for u in saved_utterances
            if u.get("text")
        ]
        if not submissions:
            await _send_error(websocket, "CONFLICT", "现在没有可以纠正的回合")
            return
        await _run_turn(db, websocket, room_id, submissions, clarification=clarification)


async def _handle_pause(
    db: AsyncSession,
    websocket: WebSocket,
    room_id: str,
    player_id: str,
    paused: bool,
) -> None:
    """暂停 / 恢复（`exec/35`）：「大家在休息」。

    🔴 **任何人都能按，任何人都能恢复**——聚会里要去接个电话的不一定是房主，
    而回来的人也不该等房主点一下。自用场景不需要权限模型。

    暂停期间：世界心跳不推进（`heartbeat.maybe_fire_room`）、行动提交被挡回。
    **讨论区照常**——休息时聊天正是它的用途（`exec/18` 的判据：讨论区是玩家
    之间的通道，不推进世界）。

    ⚠️ 已经在跑的那一轮**不会被打断**：它已经调出去了，叙事收不回来。
    暂停的语义是"别开新的"，不是"取消当前的"。
    """
    room = await room_service.find_room_by_id(db, room_id)
    if room is None or room.paused == paused:
        return
    room.paused = paused
    await db.commit()
    player = await room_service.get_player(db, player_id)
    nickname = player.nickname if player is not None else "有人"
    # 元层信息（谁按了暂停），不含虚构内容 → 全房间，同 `exec/33 §3.3`
    await manager.broadcast(
        room_id,
        ServerEnvelope(
            type="room.paused",
            payload=RoomPausedPayload(paused=paused, by_nickname=nickname).model_dump(
                by_alias=True
            ),
        ).model_dump(by_alias=True),
    )


async def _handle_adjourn(
    db: AsyncSession,
    websocket: WebSocket,
    room_id: str,
    player_id: str,
    adjourned: bool,
) -> None:
    """今晚到此为止 / 下次接着跑（`exec/46` B3）。

    🔴 **只有房主能按**，跟 `_handle_pause` 相反。理由是两者的代价不同：休息
    随手就能恢复，散会要留下一段「上次讲到哪」、要开一行新的场次记录，是这一
    局的节拍。用户 2026-08-24 定的。

    世界状态一个字都不动——续跑要做的只是把桌子重新打开（`keeper_state`、
    待掷队列、历史全都在库里躺着）。
    """
    try:
        if adjourned:
            await room_service.adjourn_session(db, room_id, player_id)
        else:
            await room_service.resume_session(db, room_id, player_id)
    except room_service.RoomAuthorizationError:
        await _send_error(websocket, "FORBIDDEN", "只有房主可以收工或继续")
        return
    except room_service.RoomConflictError as exc:
        await _send_error(websocket, "CONFLICT", str(exc))
        return
    except room_service.RoomNotFoundError:
        return

    player = await room_service.get_player(db, player_id)
    nickname = player.nickname if player is not None else "房主"
    # 🔴 「上次讲到哪」在**续跑这一下**生成并随推送发出去，收工那下不生成。
    #    理由见 `RoomAdjournedPayload.recap_text`：它是念给全桌听的开场白。
    #    房主按下「继续」要等一次 LLM 往返（约几秒）——而那正是念开场白的时刻。
    recap = None if adjourned else await session_recap.build_session_recap(db, room_id)
    # 元层信息（谁收的工），不含虚构内容 → 全房间，同 `_handle_pause`。
    await manager.broadcast(
        room_id,
        ServerEnvelope(
            type="room.adjourned",
            payload=RoomAdjournedPayload(
                adjourned=adjourned,
                by_nickname=nickname,
                session_count=await table_session.session_count(db, room_id),
                recap_text=recap,
            ).model_dump(by_alias=True),
        ).model_dump(by_alias=True),
    )


async def _handle_action_submit(
    db: AsyncSession,
    websocket: WebSocket,
    room_id: str,
    player_id: str,
    utterance: str,
    *,
    private: bool = False,
) -> None:
    """处理 action.submit：玩家对 AI 主持人说的任何一句话（issue #107 定稿后
    的唯一事件——"是行动还是提问"由 AI 判断，协议层不预分类）。

    流程：并入本轮收集窗口 → 广播玩家原话（action.broadcast）→ 开窗者拿房间锁、
    等窗口、取走全部宣告 → 一次裁决 + 一次叙事 → 广播回复 → 释放锁。

    - **收集窗口（exec/14 P5.1）**：真人守秘人同时听四个人说话、然后回应一次。
      窗口内的其他提交**并入同一轮**而不是被拒——汇总必须发生在裁决之前。
      单人局窗口为 0，行为与本功能上线前逐字一致（见 service/turn_window.py）。
    - 锁：同一房间仍然只允许一个「读状态→跑 AI→写回」循环。但**锁被占用不再是
      拒绝**（exec/19 #36）：那几条留在缓冲里，持锁的循环跑完一轮会回来接着跑
      下一轮。真人桌上不存在"你这句无效、请重说"——你说出口的话在空气里。
      finally 只释放锁、**不清缓冲**；锁自身的超时兜底见 action_lock.py。
    - 玩家原话广播：修"聊天记录像被隔离"的 bug——此前原话只在发送方本地
      插入，其他人只能看到守秘人转述。
    - Narrator 失败（超时/网络/API 错）：只告诉发起者（error 不广播），
      其他人看到了原话但等不到回复，发起者重试即可。
    """
    # 🔴 桌子没开着就不开新的一轮。**两种停**：大家在休息（`exec/35`）与
    # 今晚散会（`exec/46` B3），判断收在 `table_is_open` 一处。
    # 挡在 `_ingest_utterance` **之前**——它会广播原话并入窗口，走过去就等于
    # 这句话已经排进队里了，恢复时会突然一起涌进来。
    room = await room_service.find_room_by_id(db, room_id)
    if room is not None and not table_is_open(room):
        message = (
            "今晚已经收工了，等房主说继续"
            if room.phase == PHASE_ADJOURNED
            else "大家在休息，恢复之后再说"
        )
        await _send_error(websocket, "CONFLICT", message)
        return

    player = await room_service.get_player(db, player_id)
    nickname = player.nickname if player is not None else "玩家"

    opened = await _ingest_utterance(db, room_id, player_id, nickname, utterance, private=private)
    if not opened:
        # 已经有人在收集本轮：并入即可，不另起一个裁决循环。
        return

    lock_token = action_lock_manager.try_acquire(room_id)
    if lock_token is None:
        # 🔴 上一轮还在跑：**不 drain**，让这几条留在缓冲里等下一轮（exec/19 #36）。
        # 真人桌上不存在"你这句无效、请重说"——你说出口的话在空气里，KP 最多说
        # "等一下，我先处理完张三"。所以这里回的是**回执**不是错误。
        # 谁来处理？当前持锁的那个循环跑完一轮后会回来看缓冲（见下面的 while）。
        #
        # 🔴 文案不说"其他人"（`exec/38 #83`）：锁改成续期之后，**单人局**第一次
        # 走得到这一支——玩家等不住补发一句，等的正是**他自己上一句**。原来那句
        # "守秘人正在回应其他人"在单人局里是句假话，而这一支恰恰是他最需要一句
        # 真话的时刻。改成同时对单人局与多人局都成立的说法。
        await _send_error(websocket, "QUEUED", "守秘人还在处理上一轮，你这句已经记下了")
        return

    # 🔴 `held` 而不是 `try/finally: release`：整条循环可以跑很久（真机撞到过
    # 单拍 229 秒），锁必须跟着一起活着，否则等不住补发一句的玩家会开出第二个
    # 并发循环（`exec/38 #83`）。缓冲照旧不清——见 `held` 退出后没有 drain。
    async with action_lock_manager.held(room_id, lock_token):
        # 一次持锁可以连跑多轮：本轮跑完若缓冲里又攒下了话，直接接着开下一轮。
        # 🔴 循环条件是**同步**的，`is_collecting` 为假到 finally 里 release 之间
        # 没有任何 await——asyncio 单线程下这就是原子的，不会出现"刚判空就有人
        # 塞进来、然后锁被释放、而没有人再来处理"的孤儿缓冲。
        while turn_window_manager.is_collecting(room_id):
            connected = manager.connection_count(room_id)
            await _await_window(room_id, connected)
            # 🔴 AI 队友在这里补话：窗口已关（真人都说完了）、裁决还没开始。
            # 位置刻意选在真人之后——反过来会变成真人跟着 AI 走，它是补位的
            # 不是主角。它的话并进**同一轮**，所以桌上只出现一段守秘人回应。
            await _join_ai_players(db, websocket, room_id)
            submissions = turn_window_manager.drain(room_id)
            if not submissions:
                break
            try:
                await _run_turn(db, websocket, room_id, submissions)
            except Exception:  # noqa: BLE001 — 一轮失败不该把排在后面的人一起丢掉
                logger.warning("turn_failed", room_id=room_id, exc_info=True)
            # 守秘人可能给 AI 发了检定：它没有连接、点不了那个按钮，得替它掷。
            # 放在锁内：resolve_check 同样是"读状态→跑 AI→写回"，不能并发。
            await _auto_roll_ai_checks(db, websocket, room_id)
    # `held` 退出只释放锁。**不要在这里 drain**——缓冲里可能正排着别人的话，
    # 清掉就是把他们的发言吞了（这正是 exec/19 #36 的病灶）。


async def _join_ai_players(db: AsyncSession, websocket: WebSocket, room_id: str) -> None:
    """问 AI 队友要不要在**讨论区**说一句（exec/21 第三层，exec/25 #60 改通道）。

    ## 🔴 为什么不再走 `action.submit`

    第一版让 AI 走跟真人**完全相同**的路径，本意是防它作弊（读剧本、跳检定）。
    真人实测（exec/25 #60）暴露了这条原则本身选错了方向：玩家问「我们能直接去
    他的地下室吗」，AI 队友答「先别急，我们看看他进屋后有什么动静」——裁决器
    这一轮收到两条**等权**发言，其中一条是明确的行动宣言，于是按行动推进了世界。
    **AI 把玩家的提问变成了一次行动。**

    > **「没有特权」和「完全平等」是两件事。它没拿到不该有的特权，却拿到了
    > 它不该有的那一半平等：推进世界的权力。**

    桌游对这件事早有成规：DM 自己操控的 PC（DMPC）是公认反模式，官方替代品
    Sidekick 靠的是**机制不对称**——「升级收益比 PC 少，正是为了让 NPC 加入队伍
    时不会盖过玩家」，而且控制权推荐归玩家。三条全在机制层，没有一条是行为规范。

    所以改成写讨论区：那条通道**不写 events 表、不进任何 LLM 上下文**
    （见 `_handle_chat_send`），AI 的话于是**结构性地**够不到裁决器——不是
    "请它别推进世界"，是它在的地方没有那个开关。同「保密靠拿不到，不是请你别说」。

    玩家要它做什么，自己在主持人频道说出来（「我和阿铁一起去地下室」）——
    **行动权只有真人有，AI 的行动权是派生的。**

    时机保持不变（收集窗口关闭后、drain 之前），它仍然是对真人刚说的那句话做
    反应。改成"叙事之后再出主意"是另一个决策，等实测觉得别扭再说。

    整块**失败即沉默**：AI 补位是锦上添花，它的模型抽风不能把真人这一轮拖垮。
    """
    from app.service.ai_turn import collect_ai_submissions

    actor = getattr(websocket.app.state, "ai_actor", None)
    try:
        submissions = await collect_ai_submissions(db, room_id, actor)
    except Exception:  # noqa: BLE001 — 同上，AI 队友的任何故障都不该炸掉真人的回合
        logger.warning("ai_players_failed", room_id=room_id, exc_info=True)
        return
    for submission in submissions:
        await _post_ai_suggestion(db, room_id, submission.player_id, submission.utterance)


async def _post_ai_suggestion(db: AsyncSession, room_id: str, player_id: str, text: str) -> None:
    """把 AI 队友的一句建议发进讨论区，并广播给全房间。

    走 `save_chat_message` 而不是自己拼 `ChatMessage`：幂等、落库形状、清理
    （`/end` 会清讨论区）三件事都跟真人的消息共用一条路径。`client_message_id`
    用 uuid4——AI 没有客户端，这个字段对它只是唯一键。
    """
    player = await room_service.get_player(db, player_id)
    if player is None:
        return
    message = await chat_service.save_chat_message(db, room_id, player_id, text, str(uuid4()))
    chat_message = ChatMessagePayload(
        message_id=message.id,
        player_id=message.player_id,
        nickname=player.nickname,
        text=message.text,
        sent_at=message.created_at,
        client_message_id=message.client_message_id,
    )
    envelope = ServerEnvelope(
        type="chat.message", payload=chat_message.model_dump(by_alias=True, mode="json")
    )
    await manager.broadcast(room_id, envelope.model_dump(by_alias=True))


#: 一轮里最多替 AI 掷几次骰。防的是"结算叙事又给它发了个新检定"无限套娃——
#: 真人靠"要不要点那个按钮"天然限流，AI 没有这道闸。
_AI_AUTO_ROLL_LIMIT = 6


async def _auto_roll_ai_checks(db: AsyncSession, websocket: WebSocket, room_id: str) -> None:
    """替 AI 队友掷掉排在队首的待掷检定（exec/21 第三层）。

    AI 没有连接，那张检定卡片永远等不到点击——而 `narrate` 有 pending 守卫，
    一个掷不出去的骰子会**把整桌卡死**在"请先完成待掷的检定"。

    只处理**队首**属于 AI 的：队首是真人时就停手，等他自己点。那位点完之后，
    `_handle_check_roll` 会再调一次本函数，排在后面的 AI 检定接着掷。

    🔴 掷骰本身仍走 `narrator.resolve_check` 的服务端权威路径，与真人逐字相同
    ——AI 没有自己掷骰的特权，只是省掉了"点击"这个它做不到的动作。
    """
    from sqlalchemy import select

    from app.core.keeper.runtime.pending import ROLL_KINDS, pending_decision_manager
    from app.models.room import Player

    if not await pending_decision_manager.has(db, room_id, ROLL_KINDS):
        return
    rows = await db.execute(
        select(Player.id).where(Player.room_id == room_id, Player.is_ai.is_(True))
    )
    ai_ids = set(rows.scalars())
    if not ai_ids:
        return

    narrator = websocket.app.state.narrator
    # AI 替自己掷骰同样要亮指示器：真人在旁边等的是**结算叙事**那十几秒。
    await _broadcast_keeper_busy(room_id, True)
    try:
        # 已经在"骰子落地"那一刻推过的检定 id。整个函数共用一个集合而不是每轮新建
        # 一个——闭包捕获循环内的变量是 B023 那类经典陷阱，而 id 本来就全局唯一。
        rolled: set[str] = set()

        async def _push(notice: CheckResultNotice) -> None:
            await _broadcast_check_result(room_id, notice, db)
            rolled.add(notice.check_request_id)

        for _ in range(_AI_AUTO_ROLL_LIMIT):
            pending = await pending_decision_manager.first(db, room_id, ROLL_KINDS)
            if pending is None or pending.player_id not in ai_ids:
                return
            try:
                # AI 的骰子同样先落地再等叙事——真人在旁边看着，没理由让他多等
                outcome = await narrator.resolve_check(
                    room_id, pending.player_id, pending.decision_id, _push
                )
            except Exception:  # noqa: BLE001 — 失败就让它留在队列里，真人可见地卡住好过静默丢骰
                logger.warning(
                    "ai_auto_roll_failed",
                    room_id=room_id,
                    player_id=pending.player_id,
                    exc_info=True,
                )
                return
            for notice in outcome.check_results:
                if notice.check_request_id in rolled:
                    continue
                await _broadcast_check_result(room_id, notice, db)
            for notice in outcome.stat_changes:
                await _broadcast_stat_change(room_id, notice, db)
            if outcome.text:
                await _broadcast_narration(db, room_id, pending.player_id, outcome.text)
            await _deliver_narration_segments(db, room_id, outcome.segments)
            for notice in outcome.check_requests:
                await _broadcast_check_request(room_id, notice, db)
            # 位置可能刚变过（分头/会合/走到图外）——把每个人自己的处境推给他
            await _push_after_turn(db, websocket, room_id)
    finally:
        # 这条路有好几个 early return（队列空、没有 AI、掷骰失败），只能靠 finally 配对。
        await _broadcast_keeper_busy(room_id, False)
    logger.warning("ai_auto_roll_limit_reached", room_id=room_id, limit=_AI_AUTO_ROLL_LIMIT)


#: 等窗口时的轮询粒度。只影响"人到齐后多久发现"，不影响窗口上限。
_WINDOW_POLL_SECONDS = 0.1


async def _await_window(room_id: str, connected_players: int) -> None:
    """等收集窗口，但**人到齐就提前收**（exec/19 #35）。

    固定 sleep 满窗口是纯白等：两人局里两个人都已经提交了，再等 2.5 秒不会
    多收到任何东西，只是让所有人多盯 2.5 秒屏幕。这里改成轮询，凑齐在场人数
    就立刻开跑。单人局窗口为 0，一次都不轮询，行为逐字不变。
    """
    window = turn_window_manager.window_seconds(connected_players)
    if window <= 0:
        return
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + window
    hard_deadline = started + turn_window_manager.WINDOW_MAX_SECONDS
    while loop.time() < deadline:
        if turn_window_manager.pending_count(room_id) >= connected_players:
            return
        # 🔴 还有人在敲字就把截止时间往后推（真人实测 2026-08-11）：2.5 秒是给
        # "同时按下发送"用的，真人第二个人才刚开始打字。封顶 `WINDOW_MAX_SECONDS`
        # ——走开去泡茶的人不该让整桌停住。
        now = loop.time()
        if turn_window_manager.someone_still_typing(room_id, now=now):
            deadline = min(now + window, hard_deadline)
        await asyncio.sleep(_WINDOW_POLL_SECONDS)


async def _run_turn(
    db: AsyncSession,
    websocket: WebSocket,
    room_id: str,
    submissions: list[Submission],
    *,
    clarification: str | None = None,
) -> None:
    """跑一轮：合并宣告 → 一次裁决 → 执行 → 叙事 → 投递。调用方持有房间锁。

    `clarification` 非空 = 这是一次**玩家纠错**（`exec/35`）：`submissions`
    是上一轮的原话（从快照重建），世界指针已经由调用方回滚过了。
    """
    # 合并成一段给裁决器看；单条时返回原话本身，单人局的 prompt 因此与
    # 收集窗口上线前逐字一致（merge_utterances 的退化保证）。
    utterance = merge_utterances(submissions)
    initiator_id = submissions[0].player_id

    # 🔴 纠错的回滚点（`exec/35`）：存下**这一轮开始之前**的世界指针与原话。
    #
    # 放在编排层而不是 KeeperAgent 里：快照是"这一轮的输入"，跟哪个 narrator
    # 在跑无关。第一版写在 agent 内部，于是 fallback narrator 的房间根本没有
    # 快照、纠错一按就报错——同族于「同一件事的两头，一头可插拔一头写死」。
    #
    # 纠错轮本身不覆盖快照：否则连点两次，第二次会回滚到第一次纠错**之后**，
    # 等于纠错只能用一次（「保留旧值 = 静默说谎」）。
    if clarification is None:
        await room_service.save_turn_snapshot(
            db,
            room_id,
            [
                {"player_id": s.player_id, "nickname": s.nickname, "text": s.utterance}
                for s in submissions
            ],
        )

    # ⚠️ 先组叙事上下文、后写事件：build_narration_context 靠"当前这条
    # 还没入库"来保证历史里不含它（见该函数 docstring 的时序约定）。
    context = await room_service.build_narration_context(db, room_id, initiator_id, utterance)
    # 本轮一起发言的人：keeper 用它决定"把谁挪到新场景"——没发言的人
    # 位置不动，否则分头探索时留在别处的人会被隔空传送走（P5.2）。
    context = replace(
        context,
        participant_ids=tuple(dict.fromkeys(s.player_id for s in submissions)),
        private_player_ids=tuple(dict.fromkeys(s.player_id for s in submissions if s.private)),
        # 逐条原话：分组叙事时门厅那段的上下文里不能出现地下室那位说了
        # 什么，合并成一段就裁不开了（P5.2d）。
        clarification=clarification,
        utterances=tuple(
            PlayerUtterance(player_id=s.player_id, nickname=s.nickname, text=s.utterance)
            for s in submissions
        ),
    )
    # 事件不在这里记：每条提交在 `_handle_action_submit` 里广播之前就已按**人**
    # 落库了（广播 payload 需要那一行的 id 做去重身份，exec/19 #42）。
    #
    # 🔴 先告诉所有人"守秘人在忙"（exec/33 §5.4）：前端的「正在思考」是**本地**
    # 在自己提交时点亮的，没发言的人看不到——分头时另一组因此是整整十几秒的
    # 黑屏。线下你至少看得见 KP 在跟别人说话。
    await _broadcast_keeper_busy(room_id, True)
    try:
        narrator = websocket.app.state.narrator
        # 🔴 先分配 id 再开流：第一条 delta 就得带着它，前端才知道后续碎片拼到
        # 哪条消息上。整段写完后用**同一个** id 落库 + 广播 narration.push。
        narration_event_id = str(uuid4())
        # 未分头走 `on_delta`（全房间一段），分头走 `segment_delta_sink`
        # （每段各有各的受众与 id）。两条互斥，由 agent 按当轮分组选。
        context = replace(
            context,
            on_delta=_narration_delta_sink(room_id, narration_event_id),
            segment_delta_sink=_segment_delta_sink_factory(room_id),
        )
        try:
            outcome = await narrator.narrate(context)
        except Exception as exc:  # 外部服务的失败面（网络/超时/API 错）就是宽的，故意宽捕获
            logger.warning("narrator_failed", room_id=room_id, error=str(exc))
            await _send_turn_failure(websocket, exc)
            # 聊天区不能静默：补一条可见兜底，避免玩家以为断线
            # ⚠️ 额度用完时**不发**这句：「再说一次」正是最不该给的建议。
            if not isinstance(exc, QuotaExceeded):
                with contextlib.suppress(Exception):  # 兜底广播失败也不再抛
                    await _broadcast_narration(
                        db,
                        room_id,
                        initiator_id,
                        "守秘人整理思路时卡了一下。请用一句更明确的行动再说一次。",
                    )
            return
        # 玩家行动重置心跳节流（路线 6）
        try:
            from app.core.keeper.runtime.heartbeat import touch_activity

            touch_activity(room_id)
        except Exception:  # noqa: BLE001 — 心跳模块不可用时不影响主路径
            pass
        # outcome.text 可能为空（两段式玩家掷骰：pending 守卫命中时守秘人只
        # 重发检定请求，那句提示走**按人裁的** segments，见 `exec/23 #76`）——
        # 空文本不广播一条空 narration.push。
        for notice in outcome.stat_changes:
            await _broadcast_stat_change(room_id, notice, db)
        if outcome.text:
            await _broadcast_narration(
                db, room_id, initiator_id, outcome.text, event_id=narration_event_id
            )
        await _deliver_narration_segments(db, room_id, outcome.segments)
        for notice in outcome.check_requests:
            await _broadcast_check_request(room_id, notice, db)
        # 🔴 玩家提议收工的那张确认卡走的正是这条路（`action.submit` → 裁决判
        # `wrap_up` → `propose_end_game`），而这份「本轮要推什么」的清单此前
        # 只在检定结算那一处写全了 ⇒ 卡建出来了、进了库、分发表也备好了，
        # **就是没人把它推出去**（2026-08-20 双人真机：两次明确说"这局结束
        # 吧"，裁决两次都判对了 `wrap_up`，玩家侧一张卡都没有）。
        # 同「逐个列出的断言/判断，加一项就漏一项」——这是第三处清单。
        for offer in outcome.player_offers:
            await _broadcast_player_offer(room_id, offer, db)
    finally:
        # 🔴 必须在 finally：叙事失败那条路径上有 early return，写在成功分支里
        # 就会让「守秘人正在忙」在所有人屏幕上永远亮着。
        await _broadcast_keeper_busy(room_id, False)
        # 位置可能刚变过（分头/会合/走到图外）——把每个人自己的处境推给他
        with contextlib.suppress(Exception):
            await _push_after_turn(db, websocket, room_id)


async def _handle_check_roll(
    db: AsyncSession,
    websocket: WebSocket,
    room_id: str,
    player_id: str,
    check_request_id: str,
    roll_value: int | None = None,
) -> None:
    """处理 check.roll/san.check.roll（issue #77 协议位，feat/keeper-agent
    落地两段式玩家掷骰）：玩家确认掷骰 → `Narrator.resolve_check` 服务端权威
    生成骰值 → 广播结果；若守秘人紧接着续写了叙事或发起了新的待掷检定
    （队列清空后 resolve_check 内部会复用 narrate()），一并广播。

    两个事件共用这一个 handler：具体是技能检定还是理智检定，由 pending 队列
    里记录的 kind 决定，不需要在这里区分——`check_request_id` 全局唯一。

    跟 action.submit 共用同一把房间锁：掷骰同样可能触发"读世界状态→跑 AI
    续写→写回"的循环，必须串行，防止和另一名玩家的提交并发读到同一份旧状态。
    但**拿不到锁时是等，不是拒**（见 `_acquire_for_small_op`）：这是玩家已经
    点下去的按钮，拒绝就等于让他再点一次。
    """
    # 🔴 「骰子在桌上」的门（`exec/46` B5）。**这一层是唯一看得到房间那行的
    #    地方**，所以门开在这里而不是更下面。
    #
    #    没开开关却带了出目，**明确拒绝、不静默忽略**：静默忽略等于玩家报了个
    #    数、系统偷偷用了另一个数，而他会以为自己那颗骰算数——那比拒绝糟得多。
    #    1–100 的范围由 `CheckRollPayload` 挡在更前面。
    if roll_value is not None:
        room = await _fresh_room(db, room_id)
        if room is None or not room.allow_manual_rolls:
            await _send_error(
                websocket,
                "MANUAL_ROLL_NOT_ALLOWED",
                "这一局的骰子由系统掷。房主可以在房间设置里打开「用桌上的骰子」。",
            )
            return

    lock_token = await _acquire_for_small_op(room_id)
    if lock_token is None:
        await _send_error(websocket, "ACTION_IN_PROGRESS", "守秘人正在处理其他玩家的行动，请稍候")
        return

    async with action_lock_manager.held(room_id, lock_token):
        await _broadcast_keeper_busy(room_id, True)
        try:
            narrator = websocket.app.state.narrator
            try:
                # 🔴 骰值先落地、叙事随后（真人实测反馈「反馈太慢」）。
                # 掷骰是纯代码毫秒级，紧跟其后的结算叙事是 10 秒级的 LLM 往返——
                # 原来两件事跑完才一次性广播，玩家点完「投掷」得盯着屏幕十几秒
                # 才看得到自己掷了多少。真人桌上骰子是当场停下的。
                # ⚠️ 回调推过的这几条要记下来，下面别再广播一遍。
                pushed: set[str] = set()

                async def _push_result(notice: CheckResultNotice) -> None:
                    await _broadcast_check_result(room_id, notice, db)
                    pushed.add(notice.check_request_id)

                outcome = await narrator.resolve_check(
                    room_id, player_id, check_request_id, _push_result, roll_value=roll_value
                )
            except NotImplementedError:
                # 非 keeper 模式（Fallback/DeepSeekNarrator）没有"待掷检定"这个
                # 概念，明确告知发起者，而不是让请求悬空等不到任何回应。
                await _send_error(websocket, "NOT_IMPLEMENTED", "服务端权威掷骰本期尚未实现")
                return
            except ValueError as exc:
                # KeeperToolError（ValueError 子类）：id 不存在/已被结算/掷错了人。
                await _send_error(websocket, "CHECK_NOT_PENDING", str(exc))
                return
            except Exception as exc:  # 与 action.submit 同理：外部服务失败面宽，故意宽捕获
                # 此时骰子可能已经掷出并落库（结算叙事的 LLM 调用失败在掷骰之后）
                # ——结果没广播成，但 keeper.check 事件在历史里，玩家重发一条
                # action.submit 后裁决器能看到结果并续上，不会丢骰。
                logger.warning("resolve_check_failed", room_id=room_id, error=str(exc))
                await _send_turn_failure(websocket, exc)
                return

            await _deliver_check_outcome(db, websocket, room_id, player_id, outcome, pushed)
        finally:
            # 🔴 只关不开是错的（2026-08-10 验证跑抓到：序列出现「开→关→关→开」）。
            # 结算叙事恰恰是最需要指示器的那十几秒——那时全房间只看得见一个骰子数字
            # 然后是十几秒静默。开在上面 try 的入口，这里配对关掉。
            await _broadcast_keeper_busy(room_id, False)


async def _deliver_check_outcome(
    db: AsyncSession,
    websocket: WebSocket,
    room_id: str,
    player_id: str,
    outcome: NarrationOutcome,
    pushed: set[str],
) -> None:
    """把一次检定结算的产物广播出去。

    掷骰（`check.roll`）与幸运决定（`luck.decide`）两条入口共用——它们本来就是
    同一条链的前后两段，各写一遍就会漏（`exec/34` 那条「一个概念被起了某个实例
    的名字」的症状正是重复实现）。
    """
    for notice in outcome.check_results:
        if notice.check_request_id in pushed:
            continue  # 骰子落地那一刻已经推过了
        await _broadcast_check_result(room_id, notice, db)
    for notice in outcome.stat_changes:
        await _broadcast_stat_change(room_id, notice, db)
    if outcome.text:
        await _broadcast_narration(db, room_id, player_id, outcome.text)
    await _deliver_narration_segments(db, room_id, outcome.segments)
    for notice in outcome.check_requests:
        await _broadcast_check_request(room_id, notice, db)
    for offer in outcome.player_offers:
        await _broadcast_player_offer(room_id, offer, db)
    # 位置可能刚变过（分头/会合/走到图外）——把每个人自己的处境推给他
    await _push_after_turn(db, websocket, room_id)
    # 这位掷完了，排在他后面的 AI 检定该轮到了（exec/21 第三层）
    await _auto_roll_ai_checks(db, websocket, room_id)


async def _handle_luck_decide(
    db: AsyncSession,
    websocket: WebSocket,
    room_id: str,
    player_id: str,
    payload: LuckDecidePayload,
) -> None:
    """处理 `luck.decide`：花 / 不花（`exec/26 #66`）。

    花了会**改写结果通知**（失败 → 成功，对抗还要重算胜负），所以这里照常走
    `_deliver_check_outcome`——玩家屏幕上那个「失败」必须当场变成「成功」。

    锁与 `check.roll` 同理：**拿不到是等，不是拒**。这是玩家已经点下去的按钮，
    拒绝只能表现为"点了没反应"。
    """
    lock_token = await _acquire_for_small_op(room_id)
    if lock_token is None:
        await _send_error(websocket, "ACTION_IN_PROGRESS", "守秘人正在处理其他玩家的行动，请稍候")
        return

    async with action_lock_manager.held(room_id, lock_token):
        await _broadcast_keeper_busy(room_id, True)
        try:
            narrator = websocket.app.state.narrator
            try:
                pushed: set[str] = set()

                async def _push_result(notice: CheckResultNotice) -> None:
                    await _broadcast_check_result(room_id, notice, db)
                    pushed.add(notice.check_request_id)

                outcome = await narrator.resolve_player_offer(
                    room_id, player_id, payload.decision_id, payload.accepted, _push_result
                )
            except NotImplementedError:
                await _send_error(websocket, "NOT_IMPLEMENTED", "幸运消费本期尚未实现")
                return
            except ValueError as exc:
                # KeeperToolError：id 不存在/已被处理/不是他的决定/幸运不够了。
                await _send_error(websocket, "LUCK_DECISION_FAILED", str(exc))
                return
            except Exception as exc:  # 同 check.roll：外部服务失败面宽，故意宽捕获
                logger.warning("resolve_luck_failed", room_id=room_id, error=str(exc))
                await _send_turn_failure(websocket, exc)
                return

            await _deliver_check_outcome(db, websocket, room_id, player_id, outcome, pushed)
        finally:
            await _broadcast_keeper_busy(room_id, False)


@router.websocket("/ws/{room_id}")
async def room_socket(websocket: WebSocket, room_id: str, token: str | None = None) -> None:
    # 鉴权只用一个短 session，用完立刻释放。**不要用一个 session 包住整条连接
    # 的生命周期**——那样会在整个 WebSocket 存续期间一直占着一个数据库连接/
    # 事务，跟并发的 HTTP 请求争抢 SQLite 的锁（在测试里表现为 HTTP 请求、或者
    # 用例结束时的建表/删表拿不到连接而死锁）。下面每条消息各开各的短 session。
    async with async_session_factory() as db:
        try:
            await auth_service.get_me(db, token)
        except auth_service.AuthenticationError:
            await websocket.close(code=_UNAUTHORIZED_CLOSE_CODE)
            return

    await websocket.accept()
    bound_player_id: str | None = None
    #: LLM 配额算在谁头上（`app/core/llm_quota.py`）。在 `room.join` 时查一次
    #: 存下来——它在整条连接里不会变，每条消息再查一遍是白费一次查询。
    #:
    #: 🔴 **绑在这个循环上，不是绑在五个回合入口上。** 所有消息都在这一个任务里
    #: 顺序处理，所以这里 `with` 一次就覆盖了全部 handler——包括明天新增的那个。
    #: 逐个入口去绑正是这个项目反复吃亏的形状（加一项就漏一项）。
    bound_user_id: str | None = None

    try:
        while True:
            raw = await websocket.receive_json()

            # 信封校验不碰数据库，放在开 session 之前。一条信封本身就不合法的
            # 消息（不是对象、type 缺失等）只丢弃这一条，不打断整条连接。
            try:
                client_envelope = ClientEnvelope.model_validate(raw)
            except ValidationError as exc:
                bad_type = raw.get("type") if isinstance(raw, dict) else None
                logger.warning("ws_invalid_message", event_type=bad_type, error=str(exc))
                continue

            event_type = client_envelope.type
            player_id = client_envelope.player_id
            raw_payload = client_envelope.payload

            # 每条消息各开一个短 session，处理完立刻释放——WebSocket 在两条消息
            # 之间等待（receive_json 阻塞）时不持有任何数据库连接。
            async with async_session_factory() as db, quota_subject(bound_user_id):
                try:
                    if event_type == "room.join":
                        join_payload = RoomJoinPayload.model_validate(raw_payload)
                        if await _handle_room_join(
                            db, websocket, room_id, player_id, join_payload.reconnect_token
                        ):
                            bound_player_id = player_id
                            bound_user_id = (
                                await room_service.user_id_of_player(db, player_id)
                                if player_id
                                else None
                            )
                        else:
                            return
                        continue

                    if bound_player_id is None:
                        # 还没完成 room.join 绑定，忽略这条消息，不让未识别身份的
                        # 连接影响房间状态。
                        continue

                    if event_type == "player.ready":
                        ready_payload = PlayerReadyPayload.model_validate(raw_payload)
                        await room_service.set_player_ready(
                            db, bound_player_id, ready_payload.ready
                        )
                    elif event_type == "game.start":
                        GameStartPayload.model_validate(raw_payload)
                        try:
                            fallback_opening = await room_service.begin_game(
                                db, room_id, bound_player_id
                            )
                        except room_service.RoomAuthorizationError as exc:
                            await _send_error(websocket, "FORBIDDEN", str(exc))
                            continue
                        except room_service.CharacterIncompleteError as exc:
                            await _send_error(websocket, "CHARACTER_INCOMPLETE", str(exc))
                            continue
                        except (
                            room_service.RoomNotFoundError,
                            room_service.RoomConflictError,
                        ) as exc:
                            await _send_error(websocket, "CONFLICT", str(exc))
                            continue
                        # 设计 05 验收：game.start 后**第一轮**走开场仪式
                        # （裁决→叙事），不是干等玩家、也不是只粘贴 script。
                        # 失败/空结果时回退 structured 粘贴，保证进局必有旁白。
                        opening_text = await _run_opening_ceremony(
                            db,
                            websocket,
                            room_id,
                            bound_player_id,
                            fallback_opening,
                        )
                        await _broadcast_narration(db, room_id, bound_player_id, opening_text)
                    elif event_type == "action.submit":
                        submit_payload = ActionSubmitPayload.model_validate(raw_payload)
                        utterance = submit_payload.utterance.strip()
                        if not utterance:
                            continue
                        # visibility="private"（exec/18 ⑥，P5.2c 落地）：原话与
                        # 结果只回给发起者，同处一地的其他人不知道他做了什么。
                        # 🔴 守秘人照常看得见并正常裁定——私密是玩家↔玩家，
                        # 不是玩家↔KP（KP 不知道就没法主持）。
                        await _handle_action_submit(
                            db,
                            websocket,
                            room_id,
                            bound_player_id,
                            utterance,
                            private=submit_payload.visibility == "private",
                        )
                    elif event_type == "player.typing":
                        # 收集窗口的输入（见 `_await_window`）。**不广播、不落库**
                        # ——它既不是虚构内容也不是审计对象，只是让窗口知道"还有人
                        # 在敲"。别的玩家不需要看见"某某正在输入"，那是另一个功能。
                        if bound_player_id is not None:
                            if bool((raw_payload or {}).get("typing", True)):
                                turn_window_manager.mark_typing(
                                    room_id,
                                    bound_player_id,
                                    now=asyncio.get_running_loop().time(),
                                )
                            else:
                                turn_window_manager.clear_typing(room_id, bound_player_id)
                    elif event_type == "chat.send":
                        chat_payload = ChatSendPayload.model_validate(raw_payload)
                        await _handle_chat_send(
                            db, websocket, room_id, bound_player_id, chat_payload
                        )
                    elif event_type == "check.roll":
                        check_roll_payload = CheckRollPayload.model_validate(raw_payload)
                        await _handle_check_roll(
                            db,
                            websocket,
                            room_id,
                            bound_player_id,
                            check_roll_payload.check_request_id,
                            check_roll_payload.roll_value,
                        )
                    elif event_type == "san.check.roll":
                        san_roll_payload = SanCheckRollPayload.model_validate(raw_payload)
                        await _handle_check_roll(
                            db,
                            websocket,
                            room_id,
                            bound_player_id,
                            san_roll_payload.check_request_id,
                        )
                    elif event_type == "luck.decide":
                        luck_payload = LuckDecidePayload.model_validate(raw_payload)
                        await _handle_luck_decide(
                            db, websocket, room_id, bound_player_id, luck_payload
                        )
                    elif event_type == "room.pause":
                        pause_payload = RoomPausePayload.model_validate(raw_payload)
                        await _handle_pause(
                            db, websocket, room_id, bound_player_id, pause_payload.paused
                        )
                    elif event_type == "room.adjourn":
                        adjourn_payload = RoomAdjournPayload.model_validate(raw_payload)
                        await _handle_adjourn(
                            db, websocket, room_id, bound_player_id, adjourn_payload.adjourned
                        )
                    elif event_type == "turn.retry":
                        # 🔴 重试 = **不带澄清的纠错**：同样回滚指针、清掉没掷的
                        # 待决定项、用上一轮的原话再跑一次。两者的区别只在于有没有
                        # 多给模型一句话，没必要各写一套（「一个概念被起了它某个
                        # 实例的名字」的反面：别给同一个动作造两条实现）。
                        await _handle_clarify(db, websocket, room_id, bound_player_id, None)
                    elif event_type == "turn.clarify":
                        clarify_payload = TurnClarifyPayload.model_validate(raw_payload)
                        await _handle_clarify(
                            db,
                            websocket,
                            room_id,
                            bound_player_id,
                            clarify_payload.clarification.strip(),
                        )
                    elif event_type == "game.end.decide":
                        end_payload = EndGameDecidePayload.model_validate(raw_payload)
                        await _handle_end_game_decide(
                            db, websocket, room_id, bound_player_id, end_payload
                        )
                    elif event_type == "party.merge.confirm":
                        PartyMergeConfirmPayload.model_validate(raw_payload)
                        await _handle_merge_confirm(db, websocket, room_id, bound_player_id)
                except ValidationError as exc:
                    # payload 层校验失败（信封 OK 但具体事件 payload 形状不对），
                    # 同样只丢弃这一条。event_type 此时必然已赋值。
                    logger.warning("ws_invalid_message", event_type=event_type, error=str(exc))
                    continue
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove(room_id, websocket)
        # 断线清理另开一个短 session：上面每条消息用的 db 作用域已经结束，
        # 这里要把玩家标记为已断开，需要一个新的会话。
        if bound_player_id is not None:
            async with async_session_factory() as db:
                await room_service.set_player_connected(db, bound_player_id, False)
