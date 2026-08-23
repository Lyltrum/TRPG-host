import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

/**
 * 「我的模组」列表的删除入口。
 *
 * ## 🔴 为什么要走组件而不是只测判据
 *
 * 这一屏的易错点是**分派**：同一个「删」按钮，连着模组的走 `deleteModule`、
 * 不连的走 `deleteModuleImport`。判断本身是一行 `Boolean(resultScenarioId)`，
 * 单独测它永远绿；真正会坏的是「按钮按下去调了哪个函数」——那是接线。
 * 「只测函数不测接线」在这个仓库里已经栽过两次。
 *
 * ## 三种记录
 *
 * - 没转成的 → 删记录（此前**根本没有删除入口**，列表被它们占满）
 * - 已就绪且模组还在 → 删模组（原有行为，不许被改坏）
 * - 已就绪但模组已被删掉 → 删记录（第三种垃圾：显示「已就绪」、点不开、删不掉）
 */

const listModuleImports = vi.fn()
const deleteModule = vi.fn()
const deleteModuleImport = vi.fn()

vi.mock('@/services/module-import', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/module-import')>('@/services/module-import')
  return {
    ...actual,
    listModuleImports: () => listModuleImports(),
    deleteModule: (id: string) => deleteModule(id),
    deleteModuleImport: (id: string) => deleteModuleImport(id),
  }
})

const { default: MyModulesPage } = await import('./MyModulesPage')

function job(over: Record<string, unknown>) {
  return {
    jobId: 'job-1',
    status: 'failed',
    stage: 'assembling',
    sourceFilename: '一份模组.pdf',
    errorMessage: '校验未通过',
    failureKinds: [],
    resultScenarioId: null,
    retriedFromJobId: null,
    pageCount: 24,
    imageCount: 0,
    charCount: 100,
    itemCount: 10,
    nodeCount: 0,
    npcCount: 0,
    endingCount: 0,
    agendaCount: 0,
    hardFailureCount: 0,
    createdAt: '2026-08-04T12:00:00Z',
    updatedAt: '2026-08-04T12:05:00Z',
    finishedAt: '2026-08-04T12:05:00Z',
    ...over,
  }
}

let container: HTMLDivElement
let root: Root

beforeEach(() => {
  container = document.createElement('div')
  document.body.appendChild(container)
  root = createRoot(container)
  listModuleImports.mockReset()
  deleteModule.mockReset()
  deleteModuleImport.mockReset()
  deleteModule.mockResolvedValue(undefined)
  deleteModuleImport.mockResolvedValue(undefined)
})

afterEach(() => {
  act(() => root.unmount())
  container.remove()
})

async function flush() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

async function mount() {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/home/modules']}>
        <MyModulesPage />
      </MemoryRouter>
    )
  })
  await flush()
}

function buttonSaying(text: string): HTMLButtonElement {
  const found = [...container.querySelectorAll('button')].find((b) =>
    (b.textContent ?? '').includes(text)
  )
  if (!found) throw new Error(`没找到写着「${text}」的按钮，页面上有：${
    [...container.querySelectorAll('button')].map((b) => b.textContent).join(' / ')
  }`)
  return found as HTMLButtonElement
}

/** 点「删掉…」→ 确认，走完两步。 */
async function deleteFirstCard(label: string) {
  await act(async () => buttonSaying(label).click())
  await act(async () => buttonSaying('确认删除').click())
  await flush()
}

it('没转成的记录能删掉，走的是删记录那条路', async () => {
  listModuleImports.mockResolvedValue([job({ status: 'failed' })])
  await mount()
  await deleteFirstCard('删掉这条记录')
  expect(deleteModuleImport).toHaveBeenCalledWith('job-1')
  expect(deleteModule).not.toHaveBeenCalled()
})

it('已就绪且模组还在的，删的是模组不是记录', async () => {
  listModuleImports.mockResolvedValue([
    job({ status: 'succeeded', resultScenarioId: 'scenario-9' }),
  ])
  await mount()
  await deleteFirstCard('删掉这份')
  expect(deleteModule).toHaveBeenCalledWith('scenario-9')
  expect(deleteModuleImport).not.toHaveBeenCalled()
})

it('模组已被删掉、只剩一条「已就绪」空壳的，也能清掉', async () => {
  listModuleImports.mockResolvedValue([job({ status: 'succeeded', resultScenarioId: null })])
  await mount()
  await deleteFirstCard('删掉这条记录')
  expect(deleteModuleImport).toHaveBeenCalledWith('job-1')
})

it('正在转的那条不给删——后台还在往它上面写', async () => {
  listModuleImports.mockResolvedValue([job({ status: 'running', stage: 'assembling' })])
  await mount()
  const labels = [...container.querySelectorAll('button')].map((b) => b.textContent ?? '')
  expect(labels.some((t) => t.includes('删掉'))).toBe(false)
})

it('删不掉时把后端那句话原样显示——它是下一步，不是"失败了"', async () => {
  listModuleImports.mockResolvedValue([
    job({ status: 'succeeded', resultScenarioId: 'scenario-9' }),
  ])
  deleteModule.mockRejectedValue(new Error('还有 2 个房间在用这份模组，先解散它们再删。'))
  await mount()
  await deleteFirstCard('删掉这份')
  expect(container.textContent).toContain('还有 2 个房间在用这份模组，先解散它们再删。')
})
