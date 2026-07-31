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
- `room.rejoin` 校验完 payload 后回一条 `error` 事件（`NOT_IMPLEMENTED`），
  不做真实的断线重连（issue #77"三处原型取舍"表格 + 决策 6）。
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

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory
from app.core.narrator import (
    CheckRequestNotice,
    CheckResultNotice,
    NarrationSegment,
    PlayerUtterance,
    StatChangeNotice,
)
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
    ErrorPayload,
    GameStartPayload,
    NarrationPushPayload,
    PlayerReadyPayload,
    RoomJoinPayload,
    RoomRejoinPayload,
    SanCheckRequestPayload,
    SanCheckResultPayload,
    SanCheckRollPayload,
    ServerEnvelope,
    SessionBoundPayload,
)
from app.service import auth as auth_service
from app.service import chat as chat_service
from app.service import room as room_service
from app.service.action_lock import action_lock_manager
from app.service.turn_window import Submission, merge_utterances, turn_window_manager
from app.service.ws_manager import manager

router = APIRouter()
logger = structlog.get_logger()

_UNAUTHORIZED_CLOSE_CODE = 4401
_NOT_FOUND_CLOSE_CODE = 4404


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    """只发给触发这次交互的那一个连接，不广播——`error` 事件是"告诉发起者
    这次请求怎么了"，不是房间广播内容（issue #77 新增）。"""
    payload = ErrorPayload(code=code, message=message)
    envelope = ServerEnvelope(type="error", payload=payload.model_dump(by_alias=True))
    await websocket.send_json(envelope.model_dump(by_alias=True))


async def _broadcast_narration(
    db: AsyncSession, room_id: str, player_id: str | None, text: str
) -> None:
    """广播一条 narration.push，并同步写一行 `events` 表——`GET
    /rooms/{roomId}/replay` 读的就是这里写入的数据（issue #77 才打通的
    EventLog 闭环，此前"不记 EventLog"是已知缺口）。
    """
    narration = NarrationPushPayload(text=text)
    envelope = ServerEnvelope(type="narration.push", payload=narration.model_dump(by_alias=True))
    await manager.broadcast(room_id, envelope.model_dump(by_alias=True))
    await room_service.record_event(db, room_id, player_id, "narration.push", {"text": text})


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
        payload = NarrationPushPayload(text=segment.text)
        envelope = ServerEnvelope(type="narration.push", payload=payload.model_dump(by_alias=True))
        await manager.send_to_players(
            room_id, list(segment.audience), envelope.model_dump(by_alias=True)
        )
        await room_service.record_event(
            db,
            room_id,
            None,
            "narration.push",
            {
                "text": segment.text,
                "audience": list(segment.audience),
                "nodeId": segment.node_id,
            },
        )


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

    from app.core.keeper.location_state import group_players, load_hidden_players
    from app.models.room import Player, Room

    room = await db.get(Room, room_id)
    keeper_state = room.keeper_state if room is not None else None
    if private or player_id in load_hidden_players(keeper_state):
        return [player_id]
    rows = await db.execute(
        select(Player.id).where(Player.room_id == room_id, Player.is_ai.is_(False))
    )
    groups = group_players(keeper_state, list(rows.scalars()))
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
    from app.core.narrator import NarrationContext

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
    if text and not text.startswith("守秘人正在等待掷骰"):
        used_llm = True
    elif script:
        text = script
        used_llm = False
    else:
        text = room_service.opening_narration_for_scenario(None)
        used_llm = False

    try:
        from app.core.keeper.heartbeat import touch_activity

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


async def _broadcast_check_request(room_id: str, notice: CheckRequestNotice) -> None:
    """广播一次"待掷检定"通知（两段式玩家掷骰）——守秘人裁决需要检定后
    随叙事一起推给房间，玩家在前端看到卡片、点击掷骰后才真正生成骰值。"""
    if notice.kind == "san":
        payload: SanCheckRequestPayload | CheckRequestPayload = SanCheckRequestPayload(
            player_id=notice.player_id,
            current_san=None,
            check_request_id=notice.check_request_id,
            reason=notice.reason or None,
        )
        event_type = "san.check.request"
    else:
        payload = CheckRequestPayload(
            player_id=notice.player_id,
            skill=notice.skill or "",
            target_value=None,
            check_request_id=notice.check_request_id,
            reason=notice.reason or None,
        )
        event_type = "check.request"
    envelope = ServerEnvelope(type=event_type, payload=payload.model_dump(by_alias=True))
    await manager.broadcast(room_id, envelope.model_dump(by_alias=True))


async def _broadcast_check_result(room_id: str, notice: CheckResultNotice) -> None:
    """广播一次检定结果（玩家点击掷骰确认后，服务端权威生成骰值的结果）。"""
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
        )
        event_type = "check.result"
    envelope = ServerEnvelope(type=event_type, payload=payload.model_dump(by_alias=True))
    await manager.broadcast(room_id, envelope.model_dump(by_alias=True))


async def _broadcast_stat_change(room_id: str, notice: StatChangeNotice) -> None:
    """广播一次 HP 变更（真人实测 09-#4 修复）：裁决判定伤害后立即执行，没有
    对应的检定/掷骰事件可以携带新值，此前只拼进叙事正文当纯文本，前端角色卡
    拿不到结构化数据。这条只广播 HP——San 已经有 `san.check.result` 携带
    `san_remaining`，走"检定→掷骰→广播结果"这条路，不需要这个事件。"""
    payload = CharacterStatChangedPayload(
        player_id=notice.player_id, hp=notice.hp, hp_max=notice.hp_max, reason=notice.reason or None
    )
    envelope = ServerEnvelope(
        type="character.stat_changed", payload=payload.model_dump(by_alias=True)
    )
    await manager.broadcast(room_id, envelope.model_dump(by_alias=True))


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
    manager.add(room_id, websocket, player_id)
    await room_service.set_player_connected(db, player_id, True)
    payload = SessionBoundPayload(room_id=room_id, player_id=player_id)
    envelope = ServerEnvelope(type="session.bound", payload=payload.model_dump(by_alias=True))
    await websocket.send_json(envelope.model_dump(by_alias=True))
    return True


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
    - 锁：窗口关闭、裁决开跑之后，同一房间仍然只允许一个「读状态→跑 AI→写回」
      循环，这时的提交才回到 ACTION_IN_PROGRESS——那时拒绝是对的，世界状态
      确实正在被改写。finally 无条件释放 + 锁自身的超时兜底（action_lock.py），
      保证一次 AI 失败不会永久锁死房间。
    - 玩家原话广播：修"聊天记录像被隔离"的 bug——此前原话只在发送方本地
      插入，其他人只能看到守秘人转述。
    - Narrator 失败（超时/网络/API 错）：只告诉发起者（error 不广播），
      其他人看到了原话但等不到回复，发起者重试即可。
    """
    player = await room_service.get_player(db, player_id)
    nickname = player.nickname if player is not None else "玩家"

    # 原话先广播：不管这条是开窗的还是并入的，同处一地的人都该**立刻**看见。
    # 分头后只发给在场的那几个（P5.2）；未分头时 audience 是 None，走原来的
    # 全房间广播，行为逐字不变。
    action_envelope = ServerEnvelope(
        type="action.broadcast",
        payload=ActionBroadcastPayload(
            player_id=player_id, nickname=nickname, utterance=utterance
        ).model_dump(by_alias=True),
    ).model_dump(by_alias=True)
    audience = await _audience_at_speaker_location(db, room_id, player_id, private=private)
    if audience is None:
        await manager.broadcast(room_id, action_envelope)
    else:
        await manager.send_to_players(room_id, audience, action_envelope)

    opened = turn_window_manager.join(
        room_id,
        Submission(player_id=player_id, nickname=nickname, utterance=utterance, private=private),
    )
    if not opened:
        # 已经有人在收集本轮：并入即可，不另起一个裁决循环。
        return

    lock_token = action_lock_manager.try_acquire(room_id)
    if lock_token is None:
        # 上一轮的裁决还在跑（窗口已关）——这时拒绝是对的。
        turn_window_manager.drain(room_id)
        await _send_error(websocket, "ACTION_IN_PROGRESS", "守秘人正在处理其他玩家的行动，请稍候")
        return

    try:
        window = turn_window_manager.window_seconds(manager.connection_count(room_id))
        if window > 0:
            await asyncio.sleep(window)
        submissions = turn_window_manager.drain(room_id)
        # 合并成一段给裁决器看；单条时返回原话本身，单人局的 prompt 因此与
        # 本功能上线前逐字一致（merge_utterances 的退化保证）。
        utterance = merge_utterances(submissions)

        # ⚠️ 先组叙事上下文、后写事件：build_narration_context 靠"当前这条
        # 还没入库"来保证历史里不含它（见该函数 docstring 的时序约定）。
        context = await room_service.build_narration_context(db, room_id, player_id, utterance)
        # 本轮一起发言的人：keeper 用它决定"把谁挪到新场景"——没发言的人
        # 位置不动，否则分头探索时留在别处的人会被隔空传送走（P5.2）。
        context = replace(
            context,
            participant_ids=tuple(dict.fromkeys(s.player_id for s in submissions)),
            private_player_ids=tuple(dict.fromkeys(s.player_id for s in submissions if s.private)),
            # 逐条原话：分组叙事时门厅那段的上下文里不能出现地下室那位说了
            # 什么，合并成一段就裁不开了（P5.2d）。
            utterances=tuple(
                PlayerUtterance(player_id=s.player_id, nickname=s.nickname, text=s.utterance)
                for s in submissions
            ),
        )
        # 事件按**人**分别记：账本/历史要能看出每句话是谁说的，不能只留一条
        # 合并文本（否则多人轮在历史里退化成一个匿名段落）。
        for sub in submissions:
            # 🔴 受众随事件一起落库：历史重放要能回答"这句话当时谁听见了"。
            # 没有它，P5.2d 的 per-audience 上下文裁剪就无从判断历史行的可见性
            # ——事后再猜位置是猜不回来的。不写这个键 = 公开。
            seen_by = await _audience_at_speaker_location(
                db, room_id, sub.player_id, private=sub.private
            )
            payload: dict = {"utterance": sub.utterance, "private": sub.private}
            if seen_by is not None:
                payload["audience"] = seen_by
            await room_service.record_event(db, room_id, sub.player_id, "action.submit", payload)

        narrator = websocket.app.state.narrator
        try:
            outcome = await narrator.narrate(context)
        except Exception as exc:  # 外部服务的失败面（网络/超时/API 错）就是宽的，故意宽捕获
            logger.warning("narrator_failed", room_id=room_id, error=str(exc))
            await _send_error(websocket, "INTERNAL_ERROR", "守秘人暂时无法回应，请稍后重试")
            # 聊天区不能静默：补一条可见兜底，避免玩家以为断线
            with contextlib.suppress(Exception):  # 兜底广播失败也不再抛
                await _broadcast_narration(
                    db,
                    room_id,
                    player_id,
                    "守秘人整理思路时卡了一下。请用一句更明确的行动再说一次。",
                )
            return
        # 玩家行动重置心跳节流（路线 6）
        try:
            from app.core.keeper.heartbeat import touch_activity

            touch_activity(room_id)
        except Exception:  # noqa: BLE001 — 心跳模块不可用时不影响主路径
            pass
        # outcome.text 可能为空（两段式玩家掷骰：pending 守卫命中时守秘人只
        # 重发检定请求，不产生新叙事）——空文本不广播一条空 narration.push。
        for notice in outcome.stat_changes:
            await _broadcast_stat_change(room_id, notice)
        if outcome.text:
            await _broadcast_narration(db, room_id, player_id, outcome.text)
        await _deliver_narration_segments(db, room_id, outcome.segments)
        for notice in outcome.check_requests:
            await _broadcast_check_request(room_id, notice)
    finally:
        # 🔴 两个都必须无条件释放。窗口缓冲若漏掉，房间会**永久停留在"收集中"**
        # ——之后每一条提交都只广播原话、静默不裁决，比锁死更难查（锁至少
        # 60 秒后会自己过期）。drain 是幂等的，正常路径已经取走时这里是空操作。
        turn_window_manager.drain(room_id)
        action_lock_manager.release(room_id, lock_token)


async def _handle_check_roll(
    db: AsyncSession,
    websocket: WebSocket,
    room_id: str,
    player_id: str,
    check_request_id: str,
) -> None:
    """处理 check.roll/san.check.roll（issue #77 协议位，feat/keeper-agent
    落地两段式玩家掷骰）：玩家确认掷骰 → `Narrator.resolve_check` 服务端权威
    生成骰值 → 广播结果；若守秘人紧接着续写了叙事或发起了新的待掷检定
    （队列清空后 resolve_check 内部会复用 narrate()），一并广播。

    两个事件共用这一个 handler：具体是技能检定还是理智检定，由 pending 队列
    里记录的 kind 决定，不需要在这里区分——`check_request_id` 全局唯一。

    跟 action.submit 共用同一把房间锁：掷骰同样可能触发"读世界状态→跑 AI
    续写→写回"的循环，必须串行，防止和另一名玩家的提交并发读到同一份旧状态。
    """
    lock_token = action_lock_manager.try_acquire(room_id)
    if lock_token is None:
        await _send_error(websocket, "ACTION_IN_PROGRESS", "守秘人正在处理其他玩家的行动，请稍候")
        return

    try:
        narrator = websocket.app.state.narrator
        try:
            outcome = await narrator.resolve_check(room_id, player_id, check_request_id)
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
            await _send_error(websocket, "INTERNAL_ERROR", "守秘人暂时无法回应，请稍后重试")
            return

        for notice in outcome.check_results:
            await _broadcast_check_result(room_id, notice)
        for notice in outcome.stat_changes:
            await _broadcast_stat_change(room_id, notice)
        if outcome.text:
            await _broadcast_narration(db, room_id, player_id, outcome.text)
        await _deliver_narration_segments(db, room_id, outcome.segments)
        for notice in outcome.check_requests:
            await _broadcast_check_request(room_id, notice)
    finally:
        action_lock_manager.release(room_id, lock_token)


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
            async with async_session_factory() as db:
                try:
                    if event_type == "room.join":
                        join_payload = RoomJoinPayload.model_validate(raw_payload)
                        if await _handle_room_join(
                            db, websocket, room_id, player_id, join_payload.reconnect_token
                        ):
                            bound_player_id = player_id
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
                    elif event_type == "room.rejoin":
                        RoomRejoinPayload.model_validate(raw_payload)
                        await _send_error(websocket, "NOT_IMPLEMENTED", "断线重连本期尚未实现")
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
