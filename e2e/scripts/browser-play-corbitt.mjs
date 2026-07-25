/**
 * 真人视角完整玩一局「科比特先生」——可见浏览器 + 根据 KP 旁白现场决定输入。
 * 建房/建卡只是进局手段；进 play 后的每句行动都是临场决策，不是固定台词脚本。
 *
 *   cd e2e && node scripts/browser-play-corbitt.mjs
 */
import { chromium } from 'playwright'
import { randomBytes } from 'node:crypto'

const BASE = process.env.FE_BASE || 'http://127.0.0.1:9877'
const API = process.env.API_BASE || 'http://127.0.0.1:8000/api/v1'
const MAX_TURNS = Number(process.env.PLAY_TURNS || 6)
const HOLD_MS = Number(process.env.HOLD_MS || 120000)

const log = (...a) => console.log('[play]', ...a)

/** 读主持人频道里所有「守秘人」旁白（按 DOM 顺序） */
async function readNarrations(page) {
  return page.evaluate(() => {
    const out = []
    const nodes = document.querySelectorAll('div')
    for (const el of nodes) {
      // 标题行是 11px 的「守秘人」
      if (el.childNodes.length === 1 && el.textContent?.trim() === '守秘人') {
        const bubble = el.nextElementSibling
        if (bubble && bubble.textContent) out.push(bubble.textContent.trim())
      }
    }
    // 去重保序
    const seen = new Set()
    return out.filter((t) => {
      if (!t || seen.has(t)) return false
      seen.add(t)
      return true
    })
  })
}

async function latestNarration(page) {
  const all = await readNarrations(page)
  return all[all.length - 1] || ''
}

async function waitForNewNarration(page, prev, timeoutMs = 150000) {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    // 有待掷就先处理
    const roll = page.getByRole('button', { name: /掷骰/ })
    if (await roll.isVisible().catch(() => false)) {
      log('  → 发现待掷检定，我点掷骰')
      await roll.click()
      await page.waitForTimeout(1500)
    }
    const cur = await latestNarration(page)
    if (cur && cur !== prev && cur.length > 20) return cur
    // typing 指示器消失后再等等
    await page.waitForTimeout(1500)
  }
  return null
}

async function sendAction(page, text) {
  log('  我输入：', text)
  const input = page.getByPlaceholder('对守秘人说')
  await input.click()
  await input.fill('')
  // 像人打字：分几段 type，不是瞬间 fill 完就走
  await input.pressSequentially(text, { delay: 35 })
  await page.waitForTimeout(400)
  // 发送：找输入框右侧的提交按钮
  const form = page.locator('form').filter({ has: page.getByPlaceholder('对守秘人说') })
  const btn = form.locator('button[type="submit"]').or(form.locator('button').last())
  if (await btn.count()) {
    await btn.click()
  } else {
    await input.press('Enter')
  }
}

/**
 * 作为新手调查员，根据当前局面决定下一步。
 * 返回 null 表示觉得可以收束。
 */
function decideNextAction(turn, history) {
  const last = (history[history.length - 1] || '').toLowerCase()
  const all = history.join('\n')

  // 线索记忆（极简）
  const sawPackage = /包裹|帆布|手|指|圆柱/.test(all)
  const atHouse = /门|门铃|敲门|宅|房子|进屋|走廊|门厅/.test(all)
  const hasPaper = /报纸|新闻|失踪|孩/.test(all)
  const inside = /地下室|地窖|楼梯|房间|书房|厨房|内/.test(all) && atHouse

  // 开场 / 仍在家中窗外
  if (turn === 0 || (!atHouse && !inside)) {
    if (sawPackage && turn === 0) {
      return '我把窗帘拉开一条缝，再盯紧看他搬进门的东西，有没有第二件包裹或异常痕迹。'
    }
    if (turn === 1 && !atHouse) {
      return '我披上外套出门，穿过马路走到科比特先生的宅子前院，先不敲门，绕着前院和车道看看地上有没有掉落的东西。'
    }
    if (!atHouse) {
      return '我走到他房门前，鼓起勇气按门铃，如果有人应门就自称邻居来关心一下。'
    }
  }

  // 门前 / 对话
  if (atHouse && !inside) {
    if (/开门|应门|科比特|面无表情|让进|请进|门缝/.test(last)) {
      return '我尽量表现得自然，说昨晚好像听见外面有动静，想确认邻居是否安好；同时打量他的神情、衣服和门口有没有泥土或血迹。'
    }
    if (/拒绝|关门|不在|没人|不应/.test(last)) {
      return '既然没能进屋，我先回家找最近几天的本地报纸，查有没有儿童失踪或附近异常的报道。'
    }
    return '我再敲一次门，并留意窗户里有没有灯光或人影。'
  }

  // 进屋后
  if (inside || /客厅|门厅|请坐|茶/.test(last)) {
    if (/地下室|地窖|楼梯下|禁止|锁/.test(last)) {
      return '我找借口说想借用一下洗手间或帮忙拿东西，设法靠近通往地下室的方向，看看门锁和气味。'
    }
    if (!hasPaper && turn < 4) {
      return '在屋里时我礼貌地环顾四周，注意有没有报纸、照片、奇怪的雕像或气味。'
    }
    return '我试着提出想参观一下房子（或帮他搬东西），观察他对哪些区域特别敏感。'
  }

  // 报纸线
  if (hasPaper && !inside) {
    return '根据报纸线索，我决定今晚再监视科比特的房子，看他是否深夜外出或再次搬东西。'
  }

  // 兜底：根据最后旁白做合理调查
  if (/血|尖叫|恐怖|怪物|仪式/.test(last)) {
    return '我先退到安全距离，稳住心神，再决定是报警、继续观察还是强行介入。'
  }
  if (/成功|失败|检定|骰/.test(last)) {
    return '根据刚才的结果，我继续朝最可疑的方向推进调查，不放过任何细节。'
  }

  // 后期收束动作
  if (turn >= MAX_TURNS - 1) {
    return '我整理目前掌握的线索，向守秘人确认：我现在最该优先调查的是宅内、报纸，还是继续夜间监视？'
  }

  return '我放慢动作，再仔细搜索刚才场景里我还没检查过的角落。'
}

async function bootstrapIntoPlay(page) {
  const acc = `plyr_${randomBytes(3).toString('hex')}`
  log('进局准备：注册新号', acc)

  await page.goto(`${BASE}/auth/register`, { waitUntil: 'domcontentloaded' })
  await page.getByPlaceholder('账号').fill(acc)
  await page.getByPlaceholder('密码').fill('play1234')
  await page.getByPlaceholder('昵称').fill('临时调查员')
  await page.getByRole('button', { name: '注册' }).last().click()
  await page.waitForURL('**/home**')

  await page.getByRole('button', { name: '创建房间' }).click()
  await page.getByPlaceholder('例如：阿卡姆调查团').fill('科比特·真人局')
  await page.getByRole('button', { name: '选择游戏' }).click()
  await page.getByText('跑团', { exact: false }).first().click()
  await page.getByText('克苏鲁', { exact: false }).first().click()
  await page.getByText('科比特先生', { exact: true }).first().click()
  await page.getByRole('button', { name: '创建房间' }).click()
  await page.waitForURL('**/room/lobby**')

  await page.getByRole('button', { name: '开始游戏' }).click()
  await page.waitForURL('**/room/story**')
  log('前情页——停留 2s 让你看见背景')
  await page.waitForTimeout(2000)
  await page.getByRole('button', { name: /继续/ }).click()
  await page.waitForURL('**/room/character**')

  // 建卡：UI 填姓名；完成用 API（技能分配会拖垮「玩剧本」时间）
  await page.getByPlaceholder('角色姓名').fill('艾伦·克罗斯')
  const det = page.getByText('私家侦探', { exact: false }).first()
  if (await det.count()) await det.click()

  const roomState = await page.evaluate(() => {
    const raw = sessionStorage.getItem('aidm-room')
    if (!raw) return null
    const o = JSON.parse(raw)
    return o.state || o
  })
  const token = await page.evaluate(() => localStorage.getItem('aidm_token'))
  const h = {
    Authorization: `Bearer ${token}`,
    'X-Reconnect-Token': roomState.reconnectToken,
    'Content-Type': 'application/json',
  }
  const draft = await fetch(`${API}/rooms/${roomState.roomId}/characters`, {
    method: 'POST',
    headers: h,
  }).then((r) => r.json())
  const cid = draft.data?.characterId
  if (!cid) throw new Error('draft failed: ' + JSON.stringify(draft))
  // 与后端冒烟一致：空技能 + 无职业也可 complete（避免中文技能名/信用校验挡进局）
  const patched = await fetch(`${API}/rooms/${roomState.roomId}/characters/${cid}`, {
    method: 'PATCH',
    headers: h,
    body: JSON.stringify({
      name: '艾伦·克罗斯',
      attributes: {
        STR: 50, CON: 55, POW: 55, DEX: 60, APP: 50, SIZ: 50, INT: 65, EDU: 70, LUCK: 55,
      },
      derivedStats: { HP: 10, SAN: 55, MP: 11 },
      skills: {},
      equipment: [],
      occupation: null,
      background: '住在科比特街对面的邻居调查员。',
      notes: '',
    }),
  }).then((r) => r.json())
  if (patched.success === false) throw new Error('patch failed: ' + JSON.stringify(patched))
  const completed = await fetch(`${API}/rooms/${roomState.roomId}/characters/${cid}/complete`, {
    method: 'POST',
    headers: h,
  }).then((r) => r.json())
  if (completed.success === false) throw new Error('complete failed: ' + JSON.stringify(completed))
  log('建卡 complete 成功', cid)

  // 写回 room characterId + 确保 isHost（准备页「开始游戏」依赖轮询 hasCharacter）
  await page.evaluate((id) => {
    const raw = sessionStorage.getItem('aidm-room')
    const o = JSON.parse(raw)
    if (o.state) {
      o.state.characterId = id
      o.state.isHost = true
    } else {
      o.characterId = id
      o.isHost = true
    }
    sessionStorage.setItem('aidm-room', JSON.stringify(o))
  }, cid)

  // 轮询直到房间里自己 hasCharacter=true
  for (let i = 0; i < 20; i++) {
    const preview = await fetch(`${API}/rooms/${roomState.roomCode}`, {
      headers: { Authorization: `Bearer ${token}` },
    }).then((r) => r.json())
    const me = (preview.data?.players || []).find(
      (p) => p.playerId === roomState.playerId || p.hasCharacter,
    )
    const allOk =
      (preview.data?.players || []).length > 0 &&
      (preview.data?.players || []).every((p) => p.hasCharacter)
    log(`房间成员 hasCharacter 检查 #${i + 1}`, JSON.stringify(preview.data?.players), 'allOk', allOk)
    if (allOk) break
    await page.waitForTimeout(500)
  }

  await page.goto(`${BASE}/room/ready`, { waitUntil: 'networkidle' })
  // 等「开始游戏」可点（依赖 useRoomPlayers 轮询）
  const startBtn = page.getByRole('button', { name: /开始游戏/ })
  await startBtn.waitFor({ state: 'visible', timeout: 15000 })
  for (let i = 0; i < 40; i++) {
    if (await startBtn.isEnabled()) break
    await page.waitForTimeout(500)
    if (i === 15 || i === 30) {
      log('开始按钮仍 disabled，刷新准备页…')
      await page.reload({ waitUntil: 'networkidle' })
    }
  }
  if (!(await startBtn.isEnabled())) {
    throw new Error('准备页「开始游戏」一直灰：后端 hasCharacter 或 isHost 未就绪')
  }
  await startBtn.click()
  await page.waitForURL('**/room/play**', { timeout: 20000 })
  log('已进入 play，等开场旁白…')
  await page.waitForTimeout(2500)
  return acc
}

async function main() {
  log('将弹出 Chromium 窗口——请盯着那个窗口，我会在里面自己打字玩科比特。')
  const browser = await chromium.launch({
    headless: false,
    slowMo: 280,
    args: ['--window-size=420,900'],
  })
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  page.setDefaultTimeout(30000)

  const transcript = []

  try {
    await bootstrapIntoPlay(page)

    let prev = await latestNarration(page)
    if (prev) {
      log('—— 开场 ——')
      log(prev.slice(0, 200) + (prev.length > 200 ? '…' : ''))
      transcript.push({ role: 'kp', text: prev })
    } else {
      log('开场旁白尚未出现，再等一会…')
      prev = (await waitForNewNarration(page, '', 30000)) || ''
      if (prev) {
        log(prev.slice(0, 200))
        transcript.push({ role: 'kp', text: prev })
      }
    }

    const narrHistory = prev ? [prev] : []

    for (let turn = 0; turn < MAX_TURNS; turn++) {
      log(`\n======== 第 ${turn + 1}/${MAX_TURNS} 轮（临场决策）========`)
      const action = decideNextAction(turn, narrHistory)
      if (!action) break
      transcript.push({ role: 'me', text: action })
      const before = await latestNarration(page)
      await sendAction(page, action)
      const reply = await waitForNewNarration(page, before)
      if (!reply) {
        log('本轮未等到新旁白，可能超时或出错，停止推进。')
        break
      }
      narrHistory.push(reply)
      transcript.push({ role: 'kp', text: reply })
      log('—— KP ——')
      log(reply.slice(0, 320) + (reply.length > 320 ? '…' : ''))
      await page.waitForTimeout(800)
    }

    log('\n======== 本局实录（我的输入 vs KP）========')
    for (const line of transcript) {
      log(line.role === 'me' ? '【我】' : '【KP】', line.text.slice(0, 180))
    }

    log(`\n窗口再留 ${HOLD_MS / 1000}s，方便你翻看对话。`)
    await page.waitForTimeout(HOLD_MS)
  } catch (e) {
    console.error(e)
    log('出错，窗口保留 90s')
    await page.waitForTimeout(90000)
    process.exitCode = 1
  } finally {
    await browser.close()
    log('结束')
  }
}

main()
