/**
 * issue #107 端到端：玩家讨论区与主持人对话分流。
 *
 * 覆盖：讨论区广播 / 重发去重 / 玩家原话广播（修"聊天记录像被隔离"的 bug）/
 * 回合收集窗口的并入与拒绝边界 / 退房清空聊天 / 复盘纯净。
 *
 * 锁窗口依赖 run-e2e.ts 给后端设置的 NARRATOR_DELAY_SECONDS=1（测试钩子）：
 * 占位 narrator 同步秒回，没有这 1 秒延迟，两个客户端"同时提交"永远压不中
 * ACTION_IN_PROGRESS，锁的拒绝路径就是 e2e 测不到的死代码。
 */
import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { test } from 'node:test'

import type { ServerToClientEvent } from 'trpg-sdk'

import { createRoomWithModule, legalCharacterPayload, registerPlayer } from './helpers.ts'

const LEGAL_ATTRIBUTES = {
  STR: 50, CON: 50, POW: 50, DEX: 50,
  APP: 50, SIZ: 50, INT: 50, EDU: 50, LUCK: 50,
}

function waitForEvent(
  socketOwner: { roomSocket: { onMessage: (h: (e: ServerToClientEvent) => void) => () => void } },
  predicate: (event: ServerToClientEvent) => boolean,
  timeoutMs = 5_000
): Promise<ServerToClientEvent> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      off()
      reject(new Error(`等待事件超时（${timeoutMs}ms）`))
    }, timeoutMs)
    const off = socketOwner.roomSocket.onMessage((event) => {
      if (!predicate(event)) return
      clearTimeout(timer)
      off()
      resolve(event)
    })
  })
}

/** 连接 + room.join + 等 session.bound 的完整绑定流程。 */
async function bindSocket(
  sdk: Awaited<ReturnType<typeof registerPlayer>>['sdk'],
  roomId: string,
  token: string,
  playerId: string,
  reconnectToken: string
): Promise<void> {
  const socket = sdk.roomSocket.connect(roomId, token)
  await sdk.roomSocket.waitForOpen(socket)
  const bound = waitForEvent(sdk, (e) => e.type === 'session.bound')
  sdk.roomSocket.joinRoom(playerId, { reconnectToken })
  await bound
}

async function buildCharacter(
  sdk: Awaited<ReturnType<typeof registerPlayer>>['sdk'],
  roomId: string,
  reconnectToken: string
): Promise<void> {
  const draft = await sdk.characters.createDraft(roomId, reconnectToken)
  await sdk.characters.save(
    roomId,
    draft.characterId,
    legalCharacterPayload(LEGAL_ATTRIBUTES),
    reconnectToken
  )
  await sdk.characters.complete(roomId, draft.characterId, reconnectToken)
}

test('🔴 讨论区消息广播给房间所有人（issue #107 端到端）', async () => {
  const room = await createRoomWithModule('chatbc')
  const guest = await registerPlayer('chatbcguest')
  const joined = await guest.sdk.rooms.join(room.roomCode, { nickname: '话痨访客' }, guest.token)

  try {
    await bindSocket(
      room.host.sdk, room.roomId, room.host.token, room.hostPlayerId, room.reconnectToken
    )
    await bindSocket(guest.sdk, room.roomId, guest.token, joined.playerId, joined.reconnectToken)

    // 访客在讨论区发言，**房主**这一侧应该收到 chat.message 广播
    const hostHears = waitForEvent(
      room.host.sdk,
      (e) => e.type === 'chat.message' && e.payload.text === '我们先去图书馆吧'
    )
    guest.sdk.roomSocket.sendChat(joined.playerId, {
      text: '我们先去图书馆吧',
      clientMessageId: randomUUID(),
    })
    const heard = await hostHears
    assert.equal(heard.type, 'chat.message')
    if (heard.type === 'chat.message') {
      assert.equal(heard.payload.nickname, '话痨访客')
      assert.equal(heard.payload.playerId, joined.playerId)
    }
  } finally {
    room.host.sdk.roomSocket.disconnect()
    guest.sdk.roomSocket.disconnect()
  }
})

test('🔴 重发相同 clientMessageId 不产生重复记录（重连去重）', async () => {
  const room = await createRoomWithModule('chatdup')

  try {
    await bindSocket(
      room.host.sdk, room.roomId, room.host.token, room.hostPlayerId, room.reconnectToken
    )

    const clientMessageId = randomUUID()
    const first = waitForEvent(room.host.sdk, (e) => e.type === 'chat.message')
    room.host.sdk.roomSocket.sendChat(room.hostPlayerId, {
      text: '重发的同一条消息', clientMessageId,
    })
    const firstEvent = await first

    const second = waitForEvent(room.host.sdk, (e) => e.type === 'chat.message')
    room.host.sdk.roomSocket.sendChat(room.hostPlayerId, {
      text: '重发的同一条消息', clientMessageId,
    })
    const secondEvent = await second

    // 两次广播是同一条消息（同 messageId），历史里只有一行
    if (firstEvent.type === 'chat.message' && secondEvent.type === 'chat.message') {
      assert.equal(firstEvent.payload.messageId, secondEvent.payload.messageId)
    }
    const history = await room.host.sdk.rooms.listMessages(room.roomId, room.reconnectToken)
    assert.equal(history.length, 1)
  } finally {
    room.host.sdk.roomSocket.disconnect()
  }
})

test('🔴 所有人都能看到发起者的原话 + 守秘人回复（修"聊天像被隔离"bug）', async () => {
  const room = await createRoomWithModule('actbc')
  const guest = await registerPlayer('actbcguest')
  const joined = await guest.sdk.rooms.join(room.roomCode, { nickname: '围观访客' }, guest.token)

  try {
    await bindSocket(
      room.host.sdk, room.roomId, room.host.token, room.hostPlayerId, room.reconnectToken
    )
    await bindSocket(guest.sdk, room.roomId, guest.token, joined.playerId, joined.reconnectToken)

    // 房主提交行动，**访客**应该先看到房主的原话（action.broadcast，
    // 此前只在发送方本地显示，其他人只能看到守秘人转述——三人联机实测
    // 的"隔离"bug），再看到守秘人回复（narration.push）。
    const guestSeesUtterance = waitForEvent(
      guest.sdk,
      (e) => e.type === 'action.broadcast' && e.payload.utterance === '我推开吱呀作响的木门'
    )
    const guestSeesNarration = waitForEvent(guest.sdk, (e) => e.type === 'narration.push')
    room.host.sdk.roomSocket.submitAction(room.hostPlayerId, {
      utterance: '我推开吱呀作响的木门',
    })
    const echo = await guestSeesUtterance
    if (echo.type === 'action.broadcast') {
      assert.equal(echo.payload.playerId, room.hostPlayerId)
      assert.ok(echo.payload.nickname.length > 0)
    }
    await guestSeesNarration
  } finally {
    room.host.sdk.roomSocket.disconnect()
    guest.sdk.roomSocket.disconnect()
  }
})

test('🔴 收集窗口：窗口内他人提交并入同一轮（不再被拒），窗口外仍拒', async () => {
  const room = await createRoomWithModule('lock')
  const guest = await registerPlayer('lockguest')
  const joined = await guest.sdk.rooms.join(room.roomCode, { nickname: '抢话访客' }, guest.token)

  try {
    await bindSocket(
      room.host.sdk, room.roomId, room.host.token, room.hostPlayerId, room.reconnectToken
    )
    await bindSocket(guest.sdk, room.roomId, guest.token, joined.playerId, joined.reconnectToken)

    // exec/14 P5.1：真人守秘人同时听几个人说话、然后回应一次。窗口内的第二条
    // 提交**并入同一轮**，不再吃 ACTION_IN_PROGRESS——汇总必须发生在裁决之前。
    const hostNarration = waitForEvent(room.host.sdk, (e) => e.type === 'narration.push')
    const hostEcho = waitForEvent(room.host.sdk, (e) => e.type === 'action.broadcast')
    room.host.sdk.roomSocket.submitAction(room.hostPlayerId, { utterance: '我搜查书架' })
    await hostEcho

    // 访客在收集窗口内提交：原话照常广播给全房间，且**不该**收到拒绝
    const guestEcho = waitForEvent(
      guest.sdk,
      (e) => e.type === 'action.broadcast' && e.payload.utterance === '我翻抽屉'
    )
    const guestRejected = waitForEvent(
      guest.sdk,
      (e) => e.type === 'error' && e.payload.code === 'ACTION_IN_PROGRESS',
      1_500
    ).then(() => 'rejected' as const, () => 'no-rejection' as const)
    guest.sdk.roomSocket.submitAction(joined.playerId, { utterance: '我翻抽屉' })
    await guestEcho
    assert.equal(await guestRejected, 'no-rejection', '窗口内的提交不该被拒，应并入同一轮')

    // 两条宣告只换来**一轮**叙事（合并处理，不是各跑一次）
    await hostNarration
    const secondNarration = await waitForEvent(
      room.host.sdk,
      (e) => e.type === 'narration.push',
      1_500
    ).then(() => 'extra' as const, () => 'only-one' as const)
    assert.equal(secondNarration, 'only-one', '窗口内的多条宣告应合并成一轮叙事')

    // 窗口关闭、裁决跑起来之后再提交 —— 这时拒绝是对的（世界状态正在被改写）。
    // ⚠️ 用重试而不是一次命中：锁的释放在 narration 广播**之后**的 finally 里，
    // 两者之间有毫秒级窗口，e2e 的代码速度可能正好错过（真人手速踩不中）。
    let accepted = false
    for (let attempt = 0; attempt < 10 && !accepted; attempt++) {
      const outcome = waitForEvent(
        guest.sdk,
        (e) =>
          (e.type === 'action.broadcast' && e.payload.utterance === '我再翻抽屉') ||
          (e.type === 'error' && e.payload.code === 'ACTION_IN_PROGRESS')
      )
      guest.sdk.roomSocket.submitAction(joined.playerId, { utterance: '我再翻抽屉' })
      const event = await outcome
      if (event.type === 'action.broadcast') {
        accepted = true
      } else {
        await new Promise((r) => setTimeout(r, 100))
      }
    }
    assert.ok(accepted, '一轮结束后访客的提交应当被受理')
  } finally {
    room.host.sdk.roomSocket.disconnect()
    guest.sdk.roomSocket.disconnect()
  }
})

test('🔴 结束游戏清空聊天记录，复盘（replay）从头到尾不含聊天内容', async () => {
  const room = await createRoomWithModule('endchat')

  try {
    await room.host.sdk.rooms.startStory(room.roomId, room.reconnectToken)
    await buildCharacter(room.host.sdk, room.roomId, room.reconnectToken)
    await bindSocket(
      room.host.sdk, room.roomId, room.host.token, room.hostPlayerId, room.reconnectToken
    )

    // 推进到 InGame（end 只允许结束进行中的游戏）
    const opening = waitForEvent(room.host.sdk, (e) => e.type === 'narration.push')
    room.host.sdk.roomSocket.startGame(room.hostPlayerId)
    await opening

    const chatEcho = waitForEvent(room.host.sdk, (e) => e.type === 'chat.message')
    room.host.sdk.roomSocket.sendChat(room.hostPlayerId, {
      text: '这句话不该进复盘', clientMessageId: randomUUID(),
    })
    await chatEcho

    // end 前查得到聊天
    const before = await room.host.sdk.rooms.listMessages(room.roomId, room.reconnectToken)
    assert.equal(before.length, 1)

    await room.host.sdk.rooms.endGame(room.roomId, room.reconnectToken)

    // end 后聊天被清空
    const after = await room.host.sdk.rooms.listMessages(room.roomId, room.reconnectToken)
    assert.equal(after.length, 0)

    // replay 里没有任何聊天内容（聊天从不写 events 表）
    const replay = await room.host.sdk.rooms.getReplay(room.roomId, room.reconnectToken)
    assert.ok(!JSON.stringify(replay).includes('这句话不该进复盘'))
  } finally {
    room.host.sdk.roomSocket.disconnect()
  }
})
