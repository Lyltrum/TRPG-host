/**
 * Keeper 完整端到端：
 * 1) 起后端（DeepSeek + 科比特 structured 模组）
 * 2) SDK 跑开房→建卡→开局→多轮行动+掷骰（高技能偏成功）
 * 3) 起前端，Playwright 注入会话进游玩页，截图展示 UI 过程
 * 4) 写出 artifacts 供评测
 *
 * 用法（仓库根或 e2e/）：
 *   cd e2e && npx tsx scripts/run-keeper-full-e2e.ts
 */
import { spawn, type ChildProcess } from 'node:child_process'
import { createServer } from 'node:net'
import { rmSync } from 'node:fs'
import { mkdir, writeFile, readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createTrpgSdk, type ServerToClientEvent, type TrpgSdk } from 'trpg-sdk'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.resolve(__dirname, '../..')
const BACKEND = path.join(REPO, 'trpg-backend')
const FRONTEND = path.join(REPO, 'trpg-frontend')
const ARTIFACTS = path.join(REPO, 'e2e', 'artifacts', `keeper-e2e-${Date.now()}`)

const PORT = Number(process.env.KEEPER_E2E_PORT ?? 8110)
const FRONT_PORT = Number(process.env.KEEPER_E2E_FRONT_PORT ?? 9891)
const BASE = `http://127.0.0.1:${PORT}`
const API = `${BASE}/api/v1`
const MODULE = path.join(REPO, '模组资料', '科比特先生.structured.json')

type LogLine = { t: string; kind: string; data: unknown }
const log: LogLine[] = []
function note(kind: string, data: unknown) {
  const row = { t: new Date().toISOString(), kind, data }
  log.push(row)
  const preview =
    typeof data === 'string'
      ? data.slice(0, 200)
      : JSON.stringify(data).slice(0, 240)
  console.log(`[${kind}] ${preview}`)
}

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms))
}

async function portFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const s = createServer()
    s.once('error', () => resolve(false))
    s.once('listening', () => s.close(() => resolve(true)))
    s.listen(port, '127.0.0.1')
  })
}

async function loadBackendEnv(): Promise<Record<string, string>> {
  const env: Record<string, string> = { ...process.env } as Record<string, string>
  try {
    const raw = await readFile(path.join(BACKEND, '.env'), 'utf8')
    for (const line of raw.split('\n')) {
      const m = line.match(/^([A-Z0-9_]+)=(.*)$/)
      if (!m) continue
      let v = m[2].trim()
      if (
        (v.startsWith('"') && v.endsWith('"')) ||
        (v.startsWith("'") && v.endsWith("'"))
      ) {
        v = v.slice(1, -1)
      }
      env[m[1]] = v
    }
  } catch {
    /* optional */
  }
  // 相对 trpg-backend cwd 的路径（与正式 e2e 一致）
  env.DATABASE_URL = 'sqlite+aiosqlite:///./keeper_e2e.db'
  env.KEEPER_MODULE_PATH = MODULE
  env.KEEPER_HEARTBEAT_ENABLED = 'false'
  env.ACTION_LOCK_TIMEOUT_SECONDS = '180'
  env.ENABLE_DOCS = 'true'
  env.NARRATOR_DELAY_SECONDS = '0'
  env.CORS_ORIGINS = `["http://127.0.0.1:${FRONT_PORT}","http://localhost:${FRONT_PORT}"]`
  return env
}

async function alembicUpgrade(env: Record<string, string>): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const child = spawn(path.join(BACKEND, '.venv/bin/alembic'), ['upgrade', 'head'], {
      cwd: BACKEND,
      env,
      stdio: 'inherit',
    })
    child.on('exit', (code) =>
      code === 0 ? resolve() : reject(new Error(`alembic upgrade 退出码 ${code}`))
    )
    child.on('error', reject)
  })
  note('alembic', 'upgrade head ok')
}

async function startBackend(env: Record<string, string>): Promise<ChildProcess> {
  if (!(await portFree(PORT))) {
    throw new Error(`端口 ${PORT} 已被占用`)
  }
  rmSync(path.join(BACKEND, 'keeper_e2e.db'), { force: true })
  await alembicUpgrade(env)
  const child = spawn(
    path.join(BACKEND, '.venv/bin/uvicorn'),
    ['app.main:app', '--host', '127.0.0.1', '--port', String(PORT)],
    { cwd: BACKEND, env, stdio: ['ignore', 'pipe', 'pipe'] }
  )
  let exited: number | null = null
  child.on('exit', (code) => {
    exited = code
  })
  child.stdout?.on('data', (b) => process.stdout.write(`[be] ${b}`))
  child.stderr?.on('data', (b) => process.stderr.write(`[be] ${b}`))
  const t0 = Date.now()
  while (Date.now() - t0 < 60_000) {
    if (exited !== null) {
      throw new Error(`后端启动失败，退出码 ${exited}`)
    }
    try {
      const res = await fetch(`${BASE}/api/v1/games`)
      if (res.ok) {
        note('backend_ready', { port: PORT })
        return child
      }
    } catch {
      /* retry */
    }
    await sleep(300)
  }
  throw new Error('等待后端 /api/v1/games 超时')
}

async function startFrontend(): Promise<ChildProcess> {
  if (!(await portFree(FRONT_PORT))) {
    throw new Error(`前端端口 ${FRONT_PORT} 已被占用`)
  }
  const child = spawn(
    'npm',
    ['run', 'dev', '--', '--host', '127.0.0.1', '--port', String(FRONT_PORT)],
    {
      cwd: FRONTEND,
      env: {
        ...process.env,
        VITE_API_BASE_URL: API,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    }
  )
  child.stdout?.on('data', (b) => process.stdout.write(`[fe] ${b}`))
  child.stderr?.on('data', (b) => process.stderr.write(`[fe] ${b}`))
  const t0 = Date.now()
  while (Date.now() - t0 < 60_000) {
    try {
      const res = await fetch(`http://127.0.0.1:${FRONT_PORT}/`)
      if (res.ok) {
        note('frontend_ready', { port: FRONT_PORT })
        return child
      }
    } catch {
      /* retry */
    }
    await sleep(400)
  }
  throw new Error('等待前端就绪超时')
}

function waitEvent(
  sdk: TrpgSdk,
  pred: (e: ServerToClientEvent) => boolean,
  timeoutMs = 120_000
): Promise<ServerToClientEvent> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      off()
      reject(new Error(`等待事件超时 ${timeoutMs}ms`))
    }, timeoutMs)
    const off = sdk.roomSocket.onMessage((e) => {
      if (!pred(e)) return
      clearTimeout(timer)
      off()
      resolve(e)
    })
  })
}

async function runSdkKeeperGame(env: Record<string, string>) {
  const sdk = createTrpgSdk({ baseUrl: API })
  const account = `keeper_e2e_${Date.now()}`
  const password = 'e2e-keeper-1234'
  const reg = await sdk.auth.register({
    account,
    password,
    nickname: '调查员甲',
  })
  const token = reg.token
  note('registered', { account, userId: reg.userId })

  const room = await sdk.rooms.create(
    { roomName: '科比特端到端', nickname: '调查员甲', maxPlayers: 4 },
    token
  )
  const modules = await sdk.rooms.listModules()
  await sdk.rooms.selectModule(
    room.roomId,
    { moduleId: modules[0].id, attributeGenMethod: 'point_buy' },
    room.reconnectToken
  )
  await sdk.rooms.startStory(room.roomId, room.reconnectToken)
  note('room_ready', { roomId: room.roomId, roomCode: room.roomCode })

  // 高 EDU/技能：偏成功（会计师 EDU*4 职业点 + 兴趣点）
  const draft = await sdk.characters.createDraft(room.roomId, room.reconnectToken)
  await sdk.characters.save(
    room.roomId,
    draft.characterId,
    {
      name: '阿卡姆记者',
      age: 32,
      gender: '女',
      residence: '阿卡姆',
      birthplace: '波士顿',
      attributes: {
        STR: 40,
        CON: 50,
        POW: 60,
        DEX: 50,
        APP: 50,
        SIZ: 50,
        INT: 70,
        EDU: 90,
        LUCK: 55,
      },
      derivedStats: {},
      skills: {
        'credit-rating': 40,
        'library-use': 85,
        'spot-hidden': 80,
        listen: 50,
        persuade: 40,
        accounting: 40,
      },
      equipment: [],
      occupation: '会计师',
      background: '端到端测试调查员',
      notes: '',
    },
    room.reconnectToken
  )
  await sdk.characters.complete(room.roomId, draft.characterId, room.reconnectToken)
  note('character_complete', { characterId: draft.characterId })

  const socket = sdk.roomSocket.connect(room.roomId, token)
  try {
    await sdk.roomSocket.waitForOpen(socket)
    const boundP = waitEvent(sdk, (e) => e.type === 'session.bound')
    sdk.roomSocket.joinRoom(room.playerId, { reconnectToken: room.reconnectToken })
    await boundP
    note('ws_bound', {})

    const openNarr = waitEvent(sdk, (e) => e.type === 'narration.push')
    sdk.roomSocket.startGame(room.playerId)
    const openEv = await openNarr
    note('game_start_narration', (openEv as { payload?: { text?: string } }).payload?.text)

    const rounds = [
      '我站在窗边仔细观察对街邻居科比特的动静，把细节记下来。',
      '我去镇上的报社和图书馆，查阅本地报纸过刊，寻找相关报道。',
      '根据目前线索，我去那栋宅子的温室仔细查看那些异域植物。',
      '我小声问：根据目前情况，我们优先该做什么？',
    ]

    for (let i = 0; i < rounds.length; i++) {
      const utterance = rounds[i]
      note('player_action', { round: i + 1, utterance })

      const collected: ServerToClientEvent[] = []
      const pendingIds = new Set<string>()
      const resolvedIds = new Set<string>()
      let settleTimer: ReturnType<typeof setTimeout> | undefined

      const done = new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => {
          off()
          reject(new Error(`轮次 ${i + 1} 超时`))
        }, 180_000)

        const maybeFinish = () => {
          if (pendingIds.size > 0 && [...pendingIds].every((id) => resolvedIds.has(id))) {
            // 链式检定可能紧接着再来；短暂等待
            clearTimeout(settleTimer)
            settleTimer = setTimeout(() => {
              if ([...pendingIds].every((id) => resolvedIds.has(id))) {
                clearTimeout(timer)
                off()
                resolve()
              }
            }, 2500)
          } else if (pendingIds.size === 0) {
            clearTimeout(settleTimer)
            settleTimer = setTimeout(() => {
              if (pendingIds.size === 0) {
                clearTimeout(timer)
                off()
                resolve()
              }
            }, 1500)
          }
        }

        const off = sdk.roomSocket.onMessage((e) => {
          collected.push(e)
          if (e.type === 'action.broadcast') note('action_broadcast', e.payload)
          if (e.type === 'check.request' || e.type === 'san.check.request') {
            const id = (e.payload as { checkRequestId?: string }).checkRequestId
            note('check_request', e.payload)
            if (id && !pendingIds.has(id) && !resolvedIds.has(id)) {
              pendingIds.add(id)
              if (e.type === 'san.check.request') {
                sdk.roomSocket.rollSanCheck(room.playerId, { checkRequestId: id })
              } else {
                sdk.roomSocket.rollCheck(room.playerId, { checkRequestId: id })
              }
            }
          }
          if (e.type === 'check.result' || e.type === 'san.check.result') {
            const id = (e.payload as { checkRequestId?: string }).checkRequestId
            note('check_result', e.payload)
            if (id) resolvedIds.add(id)
            maybeFinish()
          }
          if (e.type === 'narration.push') {
            note('narration', (e.payload as { text?: string }).text)
            maybeFinish()
          }
        })
      })

      sdk.roomSocket.submitAction(room.playerId, { utterance })
      await done
      note('round_done', {
        round: i + 1,
        events: collected.map((e) => e.type),
        pending: [...pendingIds],
        resolved: [...resolvedIds],
      })
    }

    return {
      account,
      password,
      token,
      userId: reg.userId,
      roomId: room.roomId,
      roomCode: room.roomCode,
      playerId: room.playerId,
      reconnectToken: room.reconnectToken,
      characterId: draft.characterId,
      nickname: '调查员甲',
    }
  } finally {
    sdk.roomSocket.disconnect()
  }
}

async function runBrowserWalkthrough(session: {
  token: string
  userId: string
  nickname: string
  roomId: string
  roomCode: string
  playerId: string
  reconnectToken: string
  characterId: string
}) {
  // 动态导入 playwright（需已 npm i -D playwright）
  const { chromium } = await import('playwright')
  const browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({
    viewport: { width: 430, height: 900 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  const shotDir = path.join(ARTIFACTS, 'screenshots')
  await mkdir(shotDir, { recursive: true })
  let n = 0
  async function shot(name: string) {
    n += 1
    const file = path.join(shotDir, `${String(n).padStart(2, '0')}-${name}.png`)
    await page.screenshot({ path: file, fullPage: true })
    note('screenshot', file)
    return file
  }

  // 注入 token + 房间会话，直接进游玩页
  await context.addInitScript(
    ({ token, room }) => {
      localStorage.setItem('aidm_token', token)
      sessionStorage.setItem(
        'aidm-room',
        JSON.stringify({
          state: {
            roomId: room.roomId,
            roomCode: room.roomCode,
            playerId: room.playerId,
            reconnectToken: room.reconnectToken,
            characterId: room.characterId,
            isHost: true,
          },
          version: 0,
        })
      )
    },
    {
      token: session.token,
      room: {
        roomId: session.roomId,
        roomCode: session.roomCode,
        playerId: session.playerId,
        reconnectToken: session.reconnectToken,
        characterId: session.characterId,
      },
    }
  )

  await page.goto(`http://127.0.0.1:${FRONT_PORT}/home`, { waitUntil: 'networkidle' })
  await sleep(1500)
  await shot('home-restored')

  await page.goto(`http://127.0.0.1:${FRONT_PORT}/room/play`, { waitUntil: 'networkidle' })
  await sleep(2500)
  await shot('play-enter')

  // 主持人频道输入（默认应是主持人）
  const input = page.locator('textarea, input[type="text"]').last()
  await input.waitFor({ timeout: 15_000 })
  await input.fill('我再观察一下温室深处的植物和工具。')
  await shot('play-typed')
  await page.keyboard.press('Enter')
  note('ui_submit', '温室观察')

  // 等叙事或掷骰按钮
  const rollBtn = page.getByRole('button', { name: /掷骰/ })
  try {
    await Promise.race([
      page.waitForSelector('text=守秘人', { timeout: 120_000 }),
      rollBtn.waitFor({ state: 'visible', timeout: 120_000 }),
    ])
  } catch {
    note('ui_wait_timeout', '未及时看到守秘人或掷骰')
  }
  await sleep(1000)
  await shot('play-after-submit')

  if (await rollBtn.isVisible().catch(() => false)) {
    await shot('play-pending-check')
    await rollBtn.click()
    note('ui_roll', true)
    await sleep(8000)
    await shot('play-after-roll')
  }

  // 再发一轮引导
  await input.fill('我们现在优先该做什么？')
  await page.keyboard.press('Enter')
  await sleep(15_000)
  await shot('play-guidance')

  await browser.close()
}

async function main() {
  await mkdir(ARTIFACTS, { recursive: true })
  const env = await loadBackendEnv()
  if (!env.DEEPSEEK_API_KEY) {
    throw new Error('缺少 DEEPSEEK_API_KEY（trpg-backend/.env）')
  }
  note('artifacts', ARTIFACTS)
  note('module', MODULE)

  let be: ChildProcess | undefined
  let fe: ChildProcess | undefined
  try {
    be = await startBackend(env)
    const session = await runSdkKeeperGame(env)
    await writeFile(
      path.join(ARTIFACTS, 'session.json'),
      JSON.stringify(session, null, 2),
      'utf8'
    )
    // SDK 段先落盘，避免前端失败丢记录
    await writeFile(path.join(ARTIFACTS, 'transcript.json'), JSON.stringify(log, null, 2), 'utf8')

    try {
      fe = await startFrontend()
      await runBrowserWalkthrough(session)
    } catch (err) {
      note('browser_error', String(err))
    }

    await writeFile(path.join(ARTIFACTS, 'transcript.json'), JSON.stringify(log, null, 2), 'utf8')

    const narrations = log.filter((l) => l.kind === 'narration').map((l) => l.data)
    const checks = log.filter((l) => l.kind === 'check_result').map((l) => l.data)
    const md = [
      '# Keeper 端到端报告',
      '',
      `- 时间：${new Date().toISOString()}`,
      `- 后端：${BASE}`,
      `- 前端：http://127.0.0.1:${FRONT_PORT}`,
      `- 产物目录：\`${ARTIFACTS}\``,
      `- 心跳：关闭`,
      '',
      '## SDK 叙事条数',
      `- narration: ${narrations.length}`,
      `- check_result: ${checks.length}`,
      '',
      '## 检定结果摘要',
      '```json',
      JSON.stringify(checks, null, 2).slice(0, 4000),
      '```',
      '',
      '## 叙事摘录（AI 生成，非模组原文）',
      ...narrations.map((t, i) => `### N${i + 1}\n\n${String(t).slice(0, 800)}\n`),
      '',
      '## 截图',
      ...(log
        .filter((l) => l.kind === 'screenshot')
        .map((l) => `- ${path.basename(String(l.data))}`)),
      '',
      '## 浏览器错误',
      ...log.filter((l) => l.kind === 'browser_error').map((l) => `- ${l.data}`),
    ].join('\n')
    await writeFile(path.join(ARTIFACTS, 'REPORT.md'), md, 'utf8')
    note('done', ARTIFACTS)
    console.log('\n=== ARTIFACTS ===\n' + ARTIFACTS)
  } finally {
    fe?.kill('SIGTERM')
    be?.kill('SIGTERM')
    await sleep(500)
  }
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
