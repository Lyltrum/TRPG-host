import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

/**
 * 复盘页的导出出口（`exec/46` B9）。
 *
 * 🔴 守的重点是**降级路**：`navigator.clipboard` 在非安全上下文里**不存在**，
 * 而这个项目的主场恰恰是局域网 `http://<内网IP>:9877`。只测"能复制"的话，
 * 真机上第一次用就会撞见一个什么都不发生的按钮。
 */

const getRoomInfo = vi.fn()
const getRoomSummary = vi.fn()

vi.mock('@/services/room', async () => {
  const actual = await vi.importActual<typeof import('@/services/room')>('@/services/room')
  return {
    ...actual,
    getRoomInfo: (code: string) => getRoomInfo(code),
    getRoomSummary: (id: string) => getRoomSummary(id),
  }
})

const { default: ReviewPage } = await import('./ReviewPage')

let container: HTMLDivElement
let root: Root
let originalClipboard: unknown

beforeEach(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  originalClipboard = navigator.clipboard
  getRoomInfo.mockResolvedValue({
    roomId: 'r1',
    roomCode: 'ABC123',
    roomName: '周五那局',
    moduleTitle: '林中屋',
    players: [{ playerId: 'p1', nickname: '小林', isHost: true }],
  })
  getRoomSummary.mockResolvedValue({
    highlights: ['掷了 17 次骰'],
    summaryText: '他们最终烧掉了那栋木屋。',
    missedTruths: ['地窖底下还有一具尸体'],
  })
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
  Object.defineProperty(navigator, 'clipboard', { value: originalClipboard, configurable: true })
})

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
  })
}

async function mount() {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/home/my-rooms/review/ABC123']}>
        <Routes>
          <Route path="/home/my-rooms/review/:roomCode" element={<ReviewPage />} />
        </Routes>
      </MemoryRouter>
    )
  })
  await flush()
}

function copyButton(): HTMLButtonElement {
  const found = [...container.querySelectorAll('button')].find((b) =>
    (b.textContent ?? '').includes('复制')
  )
  if (!found) throw new Error('没找到复制按钮')
  return found as HTMLButtonElement
}

it('复制成功时给出反馈', async () => {
  const written: string[] = []
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: (t: string) => { written.push(t); return Promise.resolve() } },
    configurable: true,
  })
  await mount()
  await act(async () => copyButton().click())
  await flush()

  expect(written).toHaveLength(1)
  expect(written[0]).toContain('【你们没查到的】')
  expect(container.textContent).toContain('已复制')
})

it('🔴 没有剪贴板 API 时把文本摊开，不是什么都不发生', async () => {
  Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
  await mount()
  await act(async () => copyButton().click())
  await flush()

  const textarea = container.querySelector('textarea')
  expect(textarea, '降级路没出现 —— 局域网 http 下这个按钮等于没反应').not.toBeNull()
  expect(textarea!.value).toContain('周五那局')
  expect(container.textContent).toContain('长按')
  expect(container.textContent).not.toContain('已复制')
})

it('摘要还在生成时不给复制——那时复制出去是半份', async () => {
  getRoomSummary.mockReturnValue(new Promise(() => {}))
  await mount()
  expect(copyButton().disabled).toBe(true)
})
