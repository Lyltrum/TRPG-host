import { DatabaseSync } from 'node:sqlite'

import { createTrpgSdk, type TrpgSdk } from 'trpg-sdk'

const BASE_URL = process.env.E2E_BASE_URL ?? 'http://127.0.0.1:8000'

export function makeSdk(): TrpgSdk {
  return createTrpgSdk({ baseUrl: `${BASE_URL}/api/v1` })
}

/** 每个用例用不同账号，避免互相干扰（数据库虽然每次是新的，但同一次运行里
 *  多个用例共用一个后端，重名会真的撞上）。 */
export function unique(prefix: string): string {
  return `${prefix}_${Date.now()}${Math.floor(Math.random() * 10_000)}`
}

export interface TestPlayer {
  sdk: TrpgSdk
  token: string
  account: string
}

export async function registerPlayer(prefix = 'e2e'): Promise<TestPlayer> {
  const sdk = makeSdk()
  const account = unique(prefix)
  const result = await sdk.auth.register({
    account,
    password: 'e2e-test-1234',
    nickname: account,
  })
  return { sdk, token: result.token, account }
}

export interface TestRoom {
  host: TestPlayer
  roomId: string
  roomCode: string
  reconnectToken: string
  hostPlayerId: string
}

/** 建一个已经选好内置模组的房间，回到「可以开始建卡」的状态。 */
export async function createRoomWithModule(
  prefix = 'e2e',
  // 复用同一个账号再开一个房间（"这一晚开第二局"）。不传就现注册一个。
  existingHost?: TestPlayer
): Promise<TestRoom> {
  const host = existingHost ?? (await registerPlayer(prefix))
  const room = await host.sdk.rooms.create(
    {
      roomName: `${prefix} 房间`,
      nickname: host.account,
      maxPlayers: 6,
    },
    host.token
  )
  const modules = await host.sdk.rooms.listModules()
  await host.sdk.rooms.selectModule(
    room.roomId,
    { moduleId: modules[0].id, attributeGenMethod: 'point_buy' },
    room.reconnectToken
  )
  return {
    host,
    roomId: room.roomId,
    roomCode: room.roomCode,
    reconnectToken: room.reconnectToken,
    hostPlayerId: room.playerId,
  }
}

/** 内置 COC7 规则系统的 ruleset——很多用例都要拿它算合法数值。 */
export async function fetchCoc7Ruleset(sdk: TrpgSdk) {
  const games = await sdk.games.list()
  for (const game of games) {
    const systems = await sdk.games.listSystems(game.id)
    const coc7 = systems.find((s) => s.name === 'COC7')
    if (coc7) return { systemId: coc7.id, ruleset: await sdk.games.getRuleset(coc7.id) }
  }
  throw new Error('种子数据里没有 COC7 规则系统')
}

/** 一张能通过 complete 校验的合法角色卡（点数购买法）。 */
export function legalCharacterPayload(attributes: Record<string, number>) {
  return {
    name: 'E2E 调查员',
    age: 32,
    gender: '女',
    residence: '阿卡姆',
    birthplace: '波士顿',
    attributes,
    derivedStats: {},
    // 会计师信用区间 [30, 70]，取下限即可——信用是必填技能，不填会被
    // CREDIT_OUT_OF_RANGE 拒。
    skills: { 'credit-rating': 30 },
    equipment: [],
    occupation: '会计师',
    background: '',
    notes: '',
  }
}

/**
 * 直接把 `keeper_state` 写进 e2e 库——**多人分头这条线唯一测得动的办法**。
 *
 * 「谁跟谁在一处」由守秘人裁决派生，而 e2e 刻意不带 API key（跑真实大模型
 * 会让结果取决于外部服务、还烧钱）。于是分头状态没有任何一条**确定性**路径
 * 造得出来。投递按位置分组，位置造不出来 = 分头相关的投递一条都测不到。
 *
 * 🔴 只用来**摆状态**，不用来断言：断言仍然只看客户端各自收到了什么字节。
 */
export function seedKeeperState(roomId: string, state: Record<string, unknown>): void {
  const db = openE2eDb()
  try {
    const result = db
      .prepare('UPDATE rooms SET keeper_state = ? WHERE id = ?')
      .run(JSON.stringify(state), storedId(roomId))
    // 🔴 改了 0 行必须炸。第一版没有这一句，UUID 形态对不上、UPDATE 静默改了
    // 0 行，于是"分头"从来没被摆起来过——而用例照样跑得通，只是**在验一个
    // 不存在的前置**。同族于「一个动作设了但什么都没发生，跟没设看起来一样」。
    if (Number(result.changes) !== 1) {
      throw new Error(`seedKeeperState 没有改到那一行（changes=${result.changes}，roomId=${roomId}）`)
    }
  } finally {
    db.close()
  }
}

/** 读回 keeper_state——调试用。 */
export function readKeeperState(roomId: string): unknown {
  const db = openE2eDb()
  try {
    const row = db.prepare('SELECT keeper_state FROM rooms WHERE id = ?').get(storedId(roomId)) as
      | { keeper_state: string | null }
      | undefined
    return row?.keeper_state ? JSON.parse(row.keeper_state) : null
  } finally {
    db.close()
  }
}

function openE2eDb(): DatabaseSync {
  const file = process.env.E2E_DB_FILE
  if (!file) throw new Error('E2E_DB_FILE 没有传进来——请检查 run-e2e.ts 的 env')
  // node:sqlite 是内置模块，不为一句 UPDATE 引第三方依赖（e2e 的依赖越少越好）。
  const db = new DatabaseSync(file)
  // 🔴 后端此刻正连着同一个库，建卡那几笔写还可能压在事务里——不等锁就是
  // `database is locked` 当场炸（第一版没这句，三条里第一条稳定红）。
  // SQLite 的默认 busy_timeout 是 0，**"不等"才是默认值**。
  db.exec('PRAGMA busy_timeout = 5000')
  return db
}

/** API 给的是带连字符的 UUID，而 `Uuid(as_uuid=False)` 在 SQLite 里存成
 *  **不带连字符**的 32 位。直接拿 API 那个值查库会一行都匹配不到。 */
function storedId(id: string): string {
  return id.replace(/-/g, '')
}

/**
 * 往**待玩家决定队列**里插一张会合确认卡（`exec/34`）。
 *
 * 它此前是 `keeper_state["待确认会合"]` 里的一个自由键，跟待掷检定各写了一套
 * （落库/推送/重连补发全都重来一遍）。收进同一个队列之后，摆前置状态也跟着
 * 换到这里——`seedKeeperState` 再也摆不出它了。
 */
export function seedMergeConfirm(roomId: string, playerId: string, nickname: string): void {
  const db = openE2eDb()
  try {
    const result = db
      .prepare(
        `INSERT INTO pending_decisions
           (decision_id, room_id, kind, player_id, player_nickname, reason, payload, created_at)
         VALUES (?, ?, 'merge_confirm', ?, ?, ?, '{}', datetime('now'))`
      )
      .run(
        `e2e-merge-${Date.now()}`,
        storedId(roomId),
        storedId(playerId),
        nickname,
        '你走到了他们那里——跟他们碰上了吗？'
      )
    // 🔴 同 seedKeeperState：改了 0 行必须炸，否则是在验一个不存在的前置。
    if (Number(result.changes) !== 1) {
      throw new Error(`seedMergeConfirm 没有插进去（changes=${result.changes}）`)
    }
  } finally {
    db.close()
  }
}
