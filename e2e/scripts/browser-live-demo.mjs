/**
 * 可见浏览器演示：注册 → 建房(科比特) → 前情 → 建卡 → 开局 → 对守秘人说话
 *
 * 用法（仓库根或 e2e）：
 *   cd e2e && node scripts/browser-live-demo.mjs
 *
 * 会弹出 Chromium 窗口（非 headless），操作放慢，方便人眼跟着看。
 * 不能操控你已打开的 Chrome 标签页——这是独立演示窗口。
 */
import { chromium } from 'playwright'
import { randomBytes } from 'node:crypto'

const BASE = process.env.FE_BASE || 'http://127.0.0.1:9877'
const API = process.env.API_BASE || 'http://127.0.0.1:8000/api/v1'
const SLOW = Number(process.env.DEMO_SLOW_MS || 450)
const ACC = `demo_${randomBytes(3).toString('hex')}`
const PASS = 'demo1234'
const NICK = '演示玩家'
const CHAR_NAME = '哈特调查员'

function log(step, msg) {
  console.log(`[demo ${step}] ${msg}`)
}

async function waitText(page, text, timeout = 20000) {
  await page.getByText(text, { exact: false }).first().waitFor({ state: 'visible', timeout })
}

async function main() {
  log(0, `启动有界面浏览器 slowMo=${SLOW}ms → ${BASE}`)
  log(0, '请盯着弹出的窗口看操作（不是你原来的 Chrome 标签）')

  const browser = await chromium.launch({
    headless: false,
    slowMo: SLOW,
    args: ['--window-size=430,900'],
  })
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()
  page.setDefaultTimeout(25000)

  try {
    // ── 1. 注册 ──
    log(1, '打开注册页')
    await page.goto(`${BASE}/auth/register`, { waitUntil: 'networkidle' })
    await page.getByPlaceholder('账号').fill(ACC)
    await page.getByPlaceholder('密码').fill(PASS)
    await page.getByPlaceholder('昵称').fill(NICK)
    await page.getByRole('button', { name: '注册' }).last().click()
    await page.waitForURL('**/home**', { timeout: 15000 })
    log(1, `已注册并进入主页 account=${ACC}`)

    // ── 2. 创建房间 + 选模组 ──
    log(2, '创建房间 → 选跑团/COC/科比特')
    await page.getByRole('button', { name: '创建房间' }).click()
    await waitText(page, '创建房间')
    await page.getByPlaceholder('例如：阿卡姆调查团').fill('演示·科比特先生')
    await page.getByRole('button', { name: '选择游戏' }).click()
    await waitText(page, '选择游戏')
    await page.getByText('跑团', { exact: false }).first().click()
    // 系统
    await page.getByText('克苏鲁', { exact: false }).first().click()
    // 模组
    await page.getByText('科比特先生', { exact: true }).first().click()
    // 回到创建页
    await waitText(page, '创建房间')
    await page.getByRole('button', { name: '创建房间' }).click()
    await page.waitForURL('**/room/lobby**', { timeout: 20000 })
    log(2, '已进大厅')

    // ── 3. 大厅开始 → 前情 ──
    log(3, '大厅点「开始游戏」进入前情')
    await page.getByRole('button', { name: '开始游戏' }).click()
    await page.waitForURL('**/room/story**', { timeout: 20000 })
    await page.waitForTimeout(1200)
    // 前情应有科比特正文
    const storyBody = await page.locator('body').innerText()
    if (storyBody.includes('科比特') || storyBody.includes('帆布') || storyBody.includes('包裹')) {
      log(3, '前情页已出现模组背景（非 DeepSeek 占位）')
    } else {
      log(3, 'WARN: 前情文案可能仍异常，body 片段: ' + storyBody.slice(0, 200).replace(/\n/g, ' '))
    }
    await page.getByRole('button', { name: /继续/ }).click()
    await page.waitForURL('**/room/character**', { timeout: 15000 })

    // ── 4. 建卡（UI 走通：姓名+职业；完成走 API 保证校验不挡演示）──
    log(4, '建卡：填姓名、选职业（可见点击），随后用 API 完成建卡保证能进局')
    await page.getByPlaceholder('角色姓名').fill(CHAR_NAME)
    // 点一个职业卡片
    const occ = page.locator('text=私家侦探').first()
    if (await occ.count()) {
      await occ.click()
    } else {
      // 任意职业名
      await page.locator('.grid.grid-cols-2 >> nth=0').click().catch(() => {})
    }
    await page.waitForTimeout(600)

    // 从 session 取房间身份，用 API complete（UI 技能分配对演示太长）
    const roomState = await page.evaluate(() => {
      const raw = sessionStorage.getItem('aidm-room')
      if (!raw) return null
      try {
        return JSON.parse(raw).state || JSON.parse(raw)
      } catch {
        return null
      }
    })
    const token = await page.evaluate(() => localStorage.getItem('aidm_token'))
    if (!roomState?.roomId || !roomState?.reconnectToken || !token) {
      throw new Error(`缺少房间/登录态 room=${JSON.stringify(roomState)} token=${!!token}`)
    }

    const apiHeaders = {
      Authorization: `Bearer ${token}`,
      'X-Reconnect-Token': roomState.reconnectToken,
      'Content-Type': 'application/json',
    }
    const draft = await fetch(`${API}/rooms/${roomState.roomId}/characters`, {
      method: 'POST',
      headers: apiHeaders,
    }).then((r) => r.json())
    const characterId = draft.data?.characterId
    if (!characterId) throw new Error('建草稿失败: ' + JSON.stringify(draft))

    await fetch(`${API}/rooms/${roomState.roomId}/characters/${characterId}`, {
      method: 'PATCH',
      headers: apiHeaders,
      body: JSON.stringify({
        name: CHAR_NAME,
        attributes: {
          STR: 50, CON: 50, POW: 50, DEX: 50, APP: 50, SIZ: 50, INT: 60, EDU: 70, LUCK: 50,
        },
        derivedStats: { HP: 10, SAN: 50, MP: 10 },
        skills: {},
        equipment: [],
        occupation: '私家侦探',
        background: '演示用调查员',
        notes: '',
      }),
    })
    const done = await fetch(
      `${API}/rooms/${roomState.roomId}/characters/${characterId}/complete`,
      { method: 'POST', headers: apiHeaders },
    ).then((r) => r.json())
    if (done.success === false) throw new Error('complete 失败: ' + JSON.stringify(done))

    // 写回 characterId，跳转准备页
    await page.evaluate((cid) => {
      const raw = sessionStorage.getItem('aidm-room')
      if (!raw) return
      const obj = JSON.parse(raw)
      if (obj.state) obj.state.characterId = cid
      else obj.characterId = cid
      sessionStorage.setItem('aidm-room', JSON.stringify(obj))
    }, characterId)

    log(4, '建卡 API 完成，跳转准备页（窗口会导航）')
    await page.goto(`${BASE}/room/ready`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(1500)

    // ── 5. 开始游戏进 play ──
    log(5, '准备页点「开始游戏」→ play')
    await page.getByRole('button', { name: /开始游戏/ }).click()
    await page.waitForURL('**/room/play**', { timeout: 20000 })
    log(5, '已进入对局，等待开场旁白回补…')
    // replay 回补开场
    await page.waitForTimeout(2500)
    let playText = await page.locator('body').innerText()
    if (
      playText.includes('科比特') ||
      playText.includes('帆布') ||
      playText.includes('包裹') ||
      playText.includes('周日')
    ) {
      log(5, '✓ 主持人区已出现科比特开场旁白')
    } else {
      log(5, 'WARN: 暂未识别开场关键词，继续尝试发言。片段: ' + playText.slice(0, 280).replace(/\n/g, ' | '))
    }

    // ── 6. 对守秘人说话 ──
    log(6, '发送行动：观察街对面…（等 KP 回应，最多 ~2 分钟）')
    const input = page.getByPlaceholder('对守秘人说')
    await input.waitFor({ state: 'visible', timeout: 10000 })
    await input.fill('我站在窗边仔细观察街对面的科比特先生，那个包裹裂开时露出了什么？')
    // 发送按钮：圆形 brass 的 send
    const sendBtn = page.locator('button').filter({ has: page.locator('svg') }).last()
    await sendBtn.click()
    log(6, '已发送，等待叙事…')

    // 等旁白变长或出现新内容
    const before = playText.length
    const deadline = Date.now() + 150000
    let gotReply = false
    while (Date.now() < deadline) {
      await page.waitForTimeout(2000)
      playText = await page.locator('body').innerText()
      if (
        playText.includes('守秘人') &&
        (playText.length > before + 80 ||
          playText.includes('路灯') ||
          playText.includes('包裹') ||
          playText.includes('手指') ||
          playText.includes('观察'))
      ) {
        // 粗判：有较长内容
        if (playText.length > before + 40) {
          gotReply = true
          break
        }
      }
    }
    if (gotReply) {
      log(6, '✓ 收到守秘人回应（窗口里应能看到旁白）')
    } else {
      log(6, 'WARN: 等待超时，请看窗口是否仍在「输入中」或报错')
    }

    log(7, '演示主流程结束。窗口将保持 90 秒供你查看，之后自动关闭。')
    log(7, `账号 ${ACC} / ${PASS} 也可你自己继续点`)
    await page.waitForTimeout(90000)
  } catch (err) {
    log('ERR', String(err))
    console.error(err)
    log('ERR', '出错了，窗口再留 60 秒方便你看现场')
    await page.waitForTimeout(60000)
    process.exitCode = 1
  } finally {
    await browser.close()
    log('done', '浏览器已关闭')
  }
}

main()
