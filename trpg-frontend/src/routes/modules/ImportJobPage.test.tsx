import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

/**
 * 🔴 重试成功后按钮必须能再点第二次。
 *
 * 真机第一轮就撞上：用户点「再试一次」→ 跳到新 job → 新 job 也失败 → 再点，
 * **请求根本没发出去**，按钮卡在「重新提交中…」。后端日志里只有一条 retry。
 *
 * 根因是路由参数变了但**路由模式没变**（`/home/modules/:jobId`），React 复用
 * 同一个组件实例、state 不重置；而 `busy` 原本只在 `catch` 里放开，成功路径上
 * 它永远是 `true`。
 *
 * 同族判据：**改了时序/导航，就要回头查依赖旧状态的代码**。这里"旧状态"是
 * 「这一屏只会看一个 job」，而重试恰恰打破了它。
 */

const getModuleImport = vi.fn()
const retryModuleImport = vi.fn()

vi.mock('@/services/module-import', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/module-import')>('@/services/module-import')
  return {
    ...actual,
    getModuleImport: (id: string) => getModuleImport(id),
    retryModuleImport: (id: string) => retryModuleImport(id),
  }
})

const { default: ImportJobPage } = await import('./ImportJobPage')

function failedJob(jobId: string) {
  return {
    jobId,
    status: 'failed',
    stage: 'assembling',
    sourceFilename: '林中屋.pdf',
    errorMessage: '校验未通过：2 处问题（numeric）',
    failureKinds: ['numeric'],
    resultScenarioId: null,
    retriedFromJobId: null,
    pageCount: 24,
    imageCount: 6,
    charCount: 16706,
    itemCount: 50,
    nodeCount: 0,
    npcCount: 0,
    endingCount: 0,
    agendaCount: 0,
    hardFailureCount: 2,
    createdAt: '2026-08-04T12:00:00Z',
    updatedAt: '2026-08-04T12:05:00Z',
    finishedAt: '2026-08-04T12:05:00Z',
  }
}

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  getModuleImport.mockReset()
  retryModuleImport.mockReset()
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
})

/** 重试按钮。**两种文案都要认**——提交中它显示的是「重新提交中…」。 */
function retryButton(): HTMLButtonElement {
  const found = [...container.querySelectorAll('button')].find((b) => {
    const t = b.textContent ?? ''
    return t.includes('再试一次') || t.includes('重新提交中')
  })
  if (!found) throw new Error('没找到重试按钮')
  return found as HTMLButtonElement
}

async function mountAt(jobId: string) {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[`/home/modules/${jobId}`]}>
        <Routes>
          <Route path="/home/modules/:jobId" element={<ImportJobPage />} />
        </Routes>
      </MemoryRouter>
    )
  })
  await flush()
}

/** 让挂载时那次异步 `load` 落地——不冲一下，第一帧还停在"读取中…"。 */
async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

it('重试成功跳到新 job 后，按钮能再点一次', async () => {
  getModuleImport.mockImplementation((id: string) => Promise.resolve(failedJob(id)))
  retryModuleImport.mockResolvedValue({ ...failedJob('job-2'), status: 'pending' })

  await mountAt('job-1')
  await act(async () => {
    retryButton().click()
  })

  // 跳到 job-2，而 job-2 这次也是失败的（真机就是这样）
  expect(retryModuleImport).toHaveBeenCalledWith('job-1')
  await flush()

  const button = retryButton()
  expect(button.disabled).toBe(false)
  expect(button.textContent).toContain('再试一次')

  await act(async () => {
    button.click()
  })
  expect(retryModuleImport).toHaveBeenCalledTimes(2)
})

it('重试请求飞在路上时按钮是禁用的', async () => {
  getModuleImport.mockImplementation((id: string) => Promise.resolve(failedJob(id)))
  let release: (v: unknown) => void = () => {}
  retryModuleImport.mockReturnValue(new Promise((r) => (release = r)))

  await mountAt('job-1')
  await act(async () => {
    retryButton().click()
  })

  const button = retryButton()
  expect(button.disabled).toBe(true)
  expect(button.textContent).toContain('重新提交中')

  await act(async () => {
    release({ ...failedJob('job-2'), status: 'pending' })
  })
})
