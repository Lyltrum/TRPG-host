import { createRoot } from 'react-dom/client'
import { act } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, type CharacterComputeResult, type SkillComputeView } from 'trpg-sdk'
import { previewCharacter } from '@/services/character/ruleset-api'
import { completeCharacter, createCharacterDraft, saveCharacter } from '@/services/character/character-api'
import { useCharacterStore } from '@/stores/character-store'
import { useRoomStore } from '@/stores/room-store'
import { useWizardSubmit } from './useWizardSubmit'
import { createInitialWizardState, type WizardState } from './wizard-state'

// 真人实测（exec/12 #30）：进游戏后角色卡 HP/SAN/MP/DB/MOV 全是 0、技能面板
// 全 0%，而数据库里的数据是对的。根因是这条**提交路径**的 previewCharacter
// 漏传 generationMethod/attributePoolTotal → 后端按点数购买法的 480 预算校验
// 掷点池角色（总和常常 >480）→ 校验失败 → 后端整体短路返回全零结果 → 全零
// 被写进 character-store。数据库对得上是因为 complete 会服务端重算，正好把
// 这个前端 bug 掩盖住了。
//
// 同一根因第三次出现（round1 修 useWizardPreview、round4 补 useWizardHydration
// 都漏了这条），所以这次补测试钉死。
vi.mock('@/services/character/ruleset-api', async (importOriginal) => ({
  // 🔴 `extractRejectedEquipment` 用**真的**：它就是被测的那半接线（从结构化
  // details 里挑出装备那几条）。整个模块 mock 掉的话，这几条测试测的是替身。
  ...(await importOriginal<typeof import('@/services/character/ruleset-api')>()),
  previewCharacter: vi.fn(),
  translateCharacterValidationError: (e: unknown) => String(e),
}))
vi.mock('@/services/character/character-api', () => ({
  createCharacterDraft: vi.fn(async () => 'char-1'),
  saveCharacter: vi.fn(async () => undefined),
  completeCharacter: vi.fn(async () => undefined),
}))
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }))

const previewCharacterMock = vi.mocked(previewCharacter)

const ruleset = {
  attributes: [],
  attributePointBuy: { budget: 480, minValue: 10, maxValue: 90, defaultValue: 50 },
  ageRange: { minValue: 15, maxValue: 89 },
  skills: [],
  occupations: [],
} as never

function computeResult(overrides: Partial<CharacterComputeResult> = {}): CharacterComputeResult {
  return {
    derivedStats: { HP: 12, SAN: 65, MP: 13, DB: '+1D4', MOV: 8 },
    occupationSkillPoints: { budget: 220, spent: 0, remaining: 220 },
    interestSkillPoints: { budget: 110, spent: 0, remaining: 110 },
    skillView: [{ id: 'spot-hidden', base: 25, allocated: 15, current: 40, cap: 90 }],
    validation: [],
    ...overrides,
  }
}

/** 掷点池角色：属性总和 495，超过点数购买法的 480 预算。 */
function rollPoolState(): WizardState {
  const state = createInitialWizardState()
  return {
    ...state,
    info: { ...state.info, name: 'dsad' },
    age: 35,
    generationMethod: 'roll_pool',
    attributePoolTotal: 495,
    attr: { STR: 65, CON: 65, POW: 65, DEX: 60, APP: 60, SIZ: 60, INT: 60, EDU: 60, LUCK: 50 },
  }
}

const skillComputeMap = new Map<string, SkillComputeView>([
  ['spot-hidden', { id: 'spot-hidden', base: 25, allocated: 0, current: 25, cap: 90 }],
])

// useWizardSubmit 是 hook，只能在渲染里调用——用一个壳组件把 handleSubmit
// 捞出来（同 useWizardPreview.test.tsx 的做法）。
function Harness({ state, expose }: { state: WizardState; expose: (fn: () => Promise<void>) => void }) {
  const { handleSubmit } = useWizardSubmit(ruleset, state, skillComputeMap, '演员')
  expose(handleSubmit)
  return null
}

async function submit(state: WizardState) {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  let fn: (() => Promise<void>) | null = null
  await act(async () => {
    root.render(<Harness state={state} expose={(f) => (fn = f)} />)
  })
  await act(async () => {
    await fn!()
  })
  root.unmount()
}

describe('useWizardSubmit', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useRoomStore.setState({ roomId: 'room-1', characterId: null })
    useCharacterStore.getState().clear()
  })

  it('最终提交的 preview 必须带上生成方式与池值总额', async () => {
    previewCharacterMock.mockResolvedValue(computeResult())
    await submit(rollPoolState())

    expect(previewCharacterMock).toHaveBeenCalledTimes(1)
    const payload = previewCharacterMock.mock.calls[0][0]
    expect(payload.generationMethod).toBe('roll_pool')
    expect(payload.attributePoolTotal).toBe(495)
  })

  it('校验通过时把权威衍生值与技能最终值写进 character-store', async () => {
    previewCharacterMock.mockResolvedValue(computeResult())
    await submit(rollPoolState())

    const saved = useCharacterStore.getState().getForRoom('room-1', null)
    expect(saved?.derived).toEqual({ hp: 12, san: 65, mp: 13, db: '+1D4', move: 8 })
    expect(saved?.skillFinalValues).toEqual({ 'spot-hidden': 40 })
  })

  it('🔴 校验不通过时中止提交，绝不把全零的降级结果写进 store', async () => {
    // 后端校验失败时返回的就是这种全零短路结果——写进去就是一张废卡。
    previewCharacterMock.mockResolvedValue(
      computeResult({
        derivedStats: {},
        skillView: [],
        occupationSkillPoints: { budget: 0, spent: 0, remaining: 0 },
        interestSkillPoints: { budget: 0, spent: 0, remaining: 0 },
        validation: [
          {
            code: 'ATTRIBUTE_POINTS_EXCEEDED',
            field: 'allocatedAttributes',
            message: '属性点总数 495 超出预算 480',
          },
        ],
      })
    )
    await submit(rollPoolState())

    expect(useCharacterStore.getState().getForRoom('room-1', null)).toBeNull()
    expect(saveCharacter).not.toHaveBeenCalled()
    expect(completeCharacter).not.toHaveBeenCalled()
    expect(createCharacterDraft).not.toHaveBeenCalled()
  })
})

// ── 装备申辩（2026-08-19）────────────────────────────────
//
// 🔴 真机量出来的：1925 年的图书管理员带把 .32 左轮被**稳定拦下 3/3**
// （会计的 .38 同样；而侦探、农夫、医生、教授的枪全部放行）。真人桌上
// 「图书管理员哪来的枪」不是主持人单方面判定，而是玩家给个理由、主持人点头。
//
// 这里守**前端这一半的接线**：被拒的那几件要能从结构化 details 里挑出来、
// 不能混进普通错误、重新提交时要把玩家写的来路带上。

/** 壳组件的加强版：把整个返回值捞出来，不只是 handleSubmit。 */
function FullHarness({
  state,
  expose,
}: {
  state: WizardState
  expose: (r: ReturnType<typeof useWizardSubmit>) => void
}) {
  expose(useWizardSubmit(ruleset, state, skillComputeMap, '演员'))
  return null
}

async function submitFull(state: WizardState, notes?: Record<string, string>) {
  const container = document.createElement('div')
  document.body.appendChild(container)
  const root = createRoot(container)
  let latest!: ReturnType<typeof useWizardSubmit>
  await act(async () => {
    root.render(<FullHarness state={state} expose={(r) => (latest = r)} />)
  })
  await act(async () => {
    await latest.handleSubmit(notes)
  })
  const result = latest
  root.unmount()
  return result
}

/** 造一个后端 422 的形状：结构化 details 里带 code/field/message。
 *
 * 🔴 **用真的 `ApiError`**：判据是 `err instanceof ApiError`，随手 `new Error`
 * 再挂上 code/details 的假货过不了那一关，而测试会因此"通过"得毫无意义
 * ——装置跟生产不同形状，连"是不是同一个类"都算形状。 */
function equipmentRejection(): ApiError {
  return new ApiError(
    'CHARACTER_INVALID',
    '角色卡未通过校验：[EQUIPMENT_IMPLAUSIBLE] 「.32 左轮手枪」…',
    422,
    [
      {
        code: 'EQUIPMENT_IMPLAUSIBLE',
        field: 'equipment..32 左轮手枪',
        message: '「.32 左轮手枪」图书管理员没有合理的持枪来源；可以改成：警哨、手杖',
      },
    ]
  )
}

describe('装备被判「拿不到」时的申辩', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useRoomStore.setState({ roomId: 'room-1', characterId: null })
    previewCharacterMock.mockResolvedValue(computeResult())
  })

  it('🔴 被拒的装备要能定位到是哪一件（读 details，不解析那句话）', async () => {
    vi.mocked(completeCharacter).mockRejectedValueOnce(equipmentRejection())

    const r = await submitFull(createInitialWizardState())

    expect(r.rejectedEquipment.map((x) => x.item)).toEqual(['.32 左轮手枪'])
    expect(r.rejectedEquipment[0].message).toContain('警哨')
  })

  it('🔴 装备被拒时**不**当成普通错误显示', async () => {
    // 它有一条走得通的出路（说明来路），而属性超标那类只能回去改。
    // 混在一起的话玩家看到「你的枪不合理」然后没有任何下一步。
    vi.mocked(completeCharacter).mockRejectedValueOnce(equipmentRejection())

    const r = await submitFull(createInitialWizardState())

    expect(r.submitError).toBe('')
  })

  it('🔴 只挑装备那一类，不是所有指向装备字段的 issue', async () => {
    // **变异检验**：把提取器里的 `code === 'EQUIPMENT_IMPLAUSIBLE'` 去掉。
    // 第一版样本只有一条 detail，两种判据挑出来的是同一条，变异体活了下来
    // ——**造的样本没走到被测分支 = 没测**。这里混一条同样指向 equipment
    // 字段、但不是"拿不到"的 issue，它没有"说明来路"这条出路。
    const err = new ApiError('CHARACTER_INVALID', '角色卡未通过校验：…', 422, [
      { code: 'EQUIPMENT_TOO_LONG', field: 'equipment.一段很长的东西', message: '写太长了' },
      {
        code: 'EQUIPMENT_IMPLAUSIBLE',
        field: 'equipment..32 左轮手枪',
        message: '「.32 左轮手枪」图书管理员没有合理的持枪来源',
      },
    ])
    vi.mocked(completeCharacter).mockRejectedValueOnce(err)

    const r = await submitFull(createInitialWizardState())

    expect(r.rejectedEquipment.map((x) => x.item)).toEqual(['.32 左轮手枪'])
  })

  it('跟装备无关的校验失败仍然照旧显示那句话', async () => {
    // 退化保证：这次改动不许把别的建卡错误吞掉。
    vi.mocked(completeCharacter).mockRejectedValueOnce(new Error('技能点没花完'))

    const r = await submitFull(createInitialWizardState())

    expect(r.rejectedEquipment).toEqual([])
    expect(r.submitError).not.toBe('')
  })

  it('🔴 重新提交时把玩家写的来路带给后端', async () => {
    // **变异检验**：把 handleSubmit 里传给 completeCharacter 的第三个参数去掉，
    // 这条当场红——那正是「加了参数没有消费方」，而两头都不会变红。
    vi.mocked(completeCharacter).mockResolvedValueOnce(undefined)
    const notes = { '.32 左轮手枪': '我父亲留下的，他是一战老兵' }

    await submitFull(createInitialWizardState(), notes)

    expect(completeCharacter).toHaveBeenCalledWith('room-1', 'char-1', notes)
  })
})
