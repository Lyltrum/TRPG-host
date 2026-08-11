/**
 * 分头之后的**投递隔离**：两个真客户端，各自收到了什么字节（`exec/33`）。
 *
 * ## 为什么这条只能写在 e2e 里
 *
 * pytest 做不了同房间双 WS 客户端（TestClient 每条连接跑在独立事件循环里，
 * 跨循环广播挂死，项目已踩过三次）。后端单测能验的只是**投递决策**——
 * `_send_to_colocated` 把信封交给了 broadcast 还是 send_to_players。
 * 「B 的 socket 上到底有没有出现那段字节」只有这里答得了。
 *
 * ## 🔴 为什么要摆 `keeper_state` 而不是走真实流程
 *
 * 「谁跟谁在一处」由守秘人裁决派生，而 e2e 刻意不带 API key。于是分头状态
 * 没有任何**确定性**路径造得出来，只能直接摆（`seedKeeperState`）。摆的是
 * 前置状态，断言仍然只看客户端收到了什么——不是拿数据库验数据库。
 *
 * ## 这条网守的是什么（2026-08-11 立）
 *
 * 多人分头那条线七跑真机里，**四跑抓到的是当天引入的缺陷，而后端全套测试
 * 每次都是绿的**。缺的从来不是"还剩几条 bug"，是**改动没有网**。
 * 手工脚本见 `e2e/scripts/mp-playtest.py`——那是工具，这里才是网。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import type { ServerToClientEvent } from 'trpg-sdk'

import {
  createRoomWithModule,
  legalCharacterPayload,
  registerPlayer,
  seedKeeperState,
} from './helpers.ts'

const LEGAL_ATTRIBUTES = {
  STR: 50, CON: 50, POW: 50, DEX: 50,
  APP: 50, SIZ: 50, INT: 50, EDU: 50, LUCK: 50,
}

/** keeper_state 里的键名——**必须与后端逐字一致**，写错了这条网会静默失效。 */
const CURRENT_NODE_KEY = '当前场景节点'
const PLAYER_LOCATION_KEY = '玩家位置'
const PENDING_MERGE_KEY = '待确认会合'
const PHASE_KEY = '对局阶段'

interface Recorder {
  events: ServerToClientEvent[]
  stop: () => void
}

/** 把这个客户端收到的**每一条**事件都记下来——"没收到"是这套用例的核心断言，
 *  只能靠全量记录 + 一段静默期来判定，不能靠等某一条。 */
function record(sdk: { roomSocket: { onMessage: (h: (e: ServerToClientEvent) => void) => () => void } }): Recorder {
  const events: ServerToClientEvent[] = []
  const stop = sdk.roomSocket.onMessage((e) => events.push(e))
  return { events, stop }
}

function waitForEvent(
  sdk: { roomSocket: { onMessage: (h: (e: ServerToClientEvent) => void) => () => void } },
  predicate: (event: ServerToClientEvent) => boolean,
  timeoutMs = 8_000
): Promise<ServerToClientEvent> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      off()
      reject(new Error(`等待事件超时（${timeoutMs}ms）`))
    }, timeoutMs)
    const off = sdk.roomSocket.onMessage((event) => {
      if (!predicate(event)) return
      clearTimeout(timer)
      off()
      resolve(event)
    })
  })
}

async function buildCharacter(
  sdk: Awaited<ReturnType<typeof registerPlayer>>['sdk'],
  roomId: string,
  reconnectToken: string
): Promise<void> {
  const draft = await sdk.characters.createDraft(roomId, reconnectToken)
  await sdk.characters.save(roomId, draft.characterId, legalCharacterPayload(LEGAL_ATTRIBUTES), reconnectToken)
  await sdk.characters.complete(roomId, draft.characterId, reconnectToken)
}

/** 建房 + 拉一个访客 + 两边建卡 + 两条 WS 都绑定好。 */
async function twoPlayersInGame(prefix: string) {
  const room = await createRoomWithModule(prefix)
  const guest = await registerPlayer(`${prefix}g`)
  const joined = await guest.sdk.rooms.join(room.roomCode, { nickname: '访客' }, guest.token)
  await room.host.sdk.rooms.startStory(room.roomId, room.reconnectToken)
  await buildCharacter(room.host.sdk, room.roomId, room.reconnectToken)
  await buildCharacter(guest.sdk, room.roomId, joined.reconnectToken)

  const hostSocket = room.host.sdk.roomSocket.connect(room.roomId, room.host.token)
  await room.host.sdk.roomSocket.waitForOpen(hostSocket)
  room.host.sdk.roomSocket.joinRoom(room.hostPlayerId, { reconnectToken: room.reconnectToken })
  await waitForEvent(room.host.sdk, (e) => e.type === 'session.bound')

  const guestSocket = guest.sdk.roomSocket.connect(room.roomId, guest.token)
  await guest.sdk.roomSocket.waitForOpen(guestSocket)
  guest.sdk.roomSocket.joinRoom(joined.playerId, { reconnectToken: joined.reconnectToken })
  await waitForEvent(guest.sdk, (e) => e.type === 'session.bound')

  return { room, guest, guestPlayerId: joined.playerId }
}

const settle = (ms = 1_500) => new Promise((r) => setTimeout(r, ms))

test('分头之后，一个人的原话不会出现在另一处那个人的连接上', async () => {
  const { room, guest, guestPlayerId } = await twoPlayersInGame('split')
  try {
    // 房主在门厅、访客在地下室。房间还没 game.start，但 action.submit 只看
    // 位置分组，不看阶段——这条用例要的就是投递那一段。
    seedKeeperState(room.roomId, {
      [PHASE_KEY]: 'investigation',
      [CURRENT_NODE_KEY]: 'hall',
      [PLAYER_LOCATION_KEY]: `${room.hostPlayerId}@hall, ${guestPlayerId}@cellar`,
    })

    const guestSaw = record(guest.sdk)
    room.host.sdk.roomSocket.submitAction(room.hostPlayerId, { utterance: '我掀开了门厅的地毯' })
    await settle()
    guestSaw.stop()

    const leaked = guestSaw.events.filter(
      (e) => JSON.stringify(e.payload ?? {}).includes('掀开了门厅的地毯')
    )
    assert.deepEqual(leaked, [], '🔴 地下室那位收到了门厅的原话')
  } finally {
    room.host.sdk.roomSocket.disconnect()
    guest.sdk.roomSocket.disconnect()
  }
})

test('未分头时原话照旧全房间广播（退化保证）', async () => {
  const { room, guest, guestPlayerId } = await twoPlayersInGame('together')
  try {
    seedKeeperState(room.roomId, {
      [PHASE_KEY]: 'investigation',
      [CURRENT_NODE_KEY]: 'hall',
      [PLAYER_LOCATION_KEY]: `${room.hostPlayerId}@hall, ${guestPlayerId}@hall`,
    })

    const heard = waitForEvent(guest.sdk, (e) =>
      JSON.stringify(e.payload ?? {}).includes('我们一起掀开地毯')
    )
    room.host.sdk.roomSocket.submitAction(room.hostPlayerId, { utterance: '我们一起掀开地毯' })
    await heard
  } finally {
    room.host.sdk.roomSocket.disconnect()
    guest.sdk.roomSocket.disconnect()
  }
})

test('会合确认之前各自成组，确认之后才并成一组', async () => {
  const { room, guest, guestPlayerId } = await twoPlayersInGame('merge')
  try {
    // 访客已经走到门厅（位置照写），但还没确认碰上——协议要求投递上仍分开。
    seedKeeperState(room.roomId, {
      [PHASE_KEY]: 'investigation',
      [CURRENT_NODE_KEY]: 'hall',
      [PLAYER_LOCATION_KEY]: `${room.hostPlayerId}@hall, ${guestPlayerId}@hall`,
      [PENDING_MERGE_KEY]: guestPlayerId,
    })

    // 确认之前：房主的原话不该到访客那边（他还挂着待确认，自己一组）
    const before = record(guest.sdk)
    room.host.sdk.roomSocket.submitAction(room.hostPlayerId, { utterance: '确认前这句话' })
    await settle()
    before.stop()
    assert.deepEqual(
      before.events.filter((e) => JSON.stringify(e.payload ?? {}).includes('确认前这句话')),
      [],
      '🔴 没确认就已经并成一组了——那是不可撤回的泄露'
    )

    // 点「已会合」→ party.update 里两个人在同一组、待确认清空
    const merged = waitForEvent(
      guest.sdk,
      (e) => e.type === 'party.update' && (e.payload?.companions as string[])?.length === 2
    )
    guest.sdk.roomSocket.confirmMerge(guestPlayerId)
    const event = await merged
    assert.equal(event.type, 'party.update')
    if (event.type !== 'party.update') return
    assert.equal(event.payload.mergePendingAt ?? null, null)

    // 确认之后：同一句话该到了
    const after = waitForEvent(guest.sdk, (e) =>
      JSON.stringify(e.payload ?? {}).includes('确认后这句话')
    )
    room.host.sdk.roomSocket.submitAction(room.hostPlayerId, { utterance: '确认后这句话' })
    await after
  } finally {
    room.host.sdk.roomSocket.disconnect()
    guest.sdk.roomSocket.disconnect()
  }
})
