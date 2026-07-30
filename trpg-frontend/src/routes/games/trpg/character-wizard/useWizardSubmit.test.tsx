import { createRoot } from 'react-dom/client'
import { act } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { CharacterComputeResult, SkillComputeView } from 'trpg-sdk'
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
vi.mock('@/services/character/ruleset-api', () => ({
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

    const saved = useCharacterStore.getState().getForRoom('room-1')
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

    expect(useCharacterStore.getState().getForRoom('room-1')).toBeNull()
    expect(saveCharacter).not.toHaveBeenCalled()
    expect(completeCharacter).not.toHaveBeenCalled()
    expect(createCharacterDraft).not.toHaveBeenCalled()
  })
})
