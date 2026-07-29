import { createRoot, type Root } from 'react-dom/client'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { CharacterComputeResult, Ruleset } from 'trpg-sdk'
import { previewCharacter } from '@/services/character/ruleset-api'
import { useWizardPreview } from './useWizardPreview'
import { createInitialWizardState, type WizardState } from './wizard-state'

// wizard-bugfix-round2.md 核心发现：`useWizardPreview` 把 `state.skillAlloc`
// （加点"增量"）直接当成 `previewCharacter()` 的 `skills` 字段发出去，但后端
// 把 `skills[id]` 解释成这个技能的最终"绝对值"。这条测试锁定修复后的行为：
// 实际发出的请求体里，技能值必须是 `base + 加点`，不是加点本身。
// vi.mock 会被 vitest 提升到文件最顶部执行，下面 `import { previewCharacter }`
// 拿到的就已经是这个 mock。
vi.mock('@/services/character/ruleset-api', () => ({
  previewCharacter: vi.fn(),
}))

const previewCharacterMock = vi.mocked(previewCharacter)

const ruleset: Ruleset = {
  attributes: [
    { key: 'STR', label: '力量', generation: '3d6*5', pointBuy: true },
    { key: 'LUCK', label: '幸运', generation: '3d6*5', pointBuy: false },
  ],
  attributePointBuy: { budget: 480, minValue: 10, maxValue: 90, defaultValue: 50 },
  ageRange: { minValue: 15, maxValue: 89 },
  skills: [],
  occupations: [],
}

function fakeComputeResult(overrides: Partial<CharacterComputeResult> = {}): CharacterComputeResult {
  return {
    derivedStats: {},
    occupationSkillPoints: { budget: 220, spent: 0, remaining: 220 },
    interestSkillPoints: { budget: 110, spent: 0, remaining: 110 },
    // 后端权威的 base=20——加点后正确的绝对值应该是 25（20+5），不是加点本身的 5。
    skillView: [{ id: 'fast-talk', base: 20, allocated: 0, current: 20, cap: 90 }],
    validation: [],
    ...overrides,
  }
}

function Harness({ state }: { state: WizardState }) {
  useWizardPreview(ruleset, state)
  return null
}

describe('useWizardPreview：加点增量必须换算成绝对值再发给后端', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    previewCharacterMock.mockReset()
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  it('第二次请求把 skillAlloc 的加点数换算成 base+加点 的绝对值', async () => {
    const baseState: WizardState = { ...createInitialWizardState(), attr: { STR: 50, LUCK: 50 } }

    // 首次请求：skillAlloc 还是空对象，不构成问题。
    previewCharacterMock.mockResolvedValueOnce(fakeComputeResult())
    root.render(<Harness state={baseState} />)
    await new Promise((resolve) => setTimeout(resolve, 450))

    expect(previewCharacterMock).toHaveBeenCalledTimes(1)
    expect(previewCharacterMock.mock.calls[0][0].skills).toEqual({})

    // 用户给 fast-talk 加了 5 点——skillAlloc 记的是这个增量。
    const withAlloc: WizardState = { ...baseState, skillAlloc: { 'fast-talk': 5 } }
    previewCharacterMock.mockResolvedValueOnce(fakeComputeResult())
    root.render(<Harness state={withAlloc} />)
    await new Promise((resolve) => setTimeout(resolve, 450))

    expect(previewCharacterMock).toHaveBeenCalledTimes(2)
    // 核心断言：发给后端的是 base(20) + 加点(5) = 25 的绝对值，不是加点本身的 5
    // （修复前这里会是 { 'fast-talk': 5 }）。
    expect(previewCharacterMock.mock.calls[1][0].skills).toEqual({ 'fast-talk': 25 })

    root.unmount()
  })

  // wizard-bugfix-round3.md / exec/12 #19：round2 的换算只在"两次连续渲染、
  // 中间 preview 已经有值"这条路径下成立。编辑已有角色卡时，水合会一次性把
  // 完整 attr + 非空 skillAlloc（增量）塞进 state，此时 preview 还是初始的
  // null——这条用例锁定这个洞已经堵上：第一次请求（引导请求）不带 skills，
  // 拿到 base 之后自动补发第二次请求，换算成正确的绝对值。
  it('水合场景：属性一次性从缺失到完整时，最终请求仍换算成绝对值', async () => {
    const incompleteState: WizardState = { ...createInitialWizardState(), attr: { STR: 50 } } // 缺 LUCK

    root.render(<Harness state={incompleteState} />)
    await new Promise((resolve) => setTimeout(resolve, 450))
    expect(previewCharacterMock).not.toHaveBeenCalled()

    // 模拟 HYDRATE：一次性把完整属性 + 已有加点（增量）塞进 state。
    const hydratedState: WizardState = {
      ...incompleteState,
      attr: { STR: 50, LUCK: 50 },
      skillAlloc: { 'fast-talk': 5 },
    }
    previewCharacterMock.mockResolvedValueOnce(fakeComputeResult()) // 第一次：引导请求，拿 base
    previewCharacterMock.mockResolvedValueOnce(fakeComputeResult()) // 第二次：补发请求，换算绝对值
    root.render(<Harness state={hydratedState} />)
    await new Promise((resolve) => setTimeout(resolve, 950))

    expect(previewCharacterMock).toHaveBeenCalledTimes(2)
    // 第一次请求此时还没有权威 base——绝不能把增量当绝对值发出去，只能不带
    // skills（核心 bug 修复前：这里会直接是 { 'fast-talk': 5 }）。
    expect(previewCharacterMock.mock.calls[0][0].skills).toEqual({})
    // 最后一次请求换算成了 base(20) + 加点(5) = 25 的绝对值。
    const lastCall = previewCharacterMock.mock.calls[previewCharacterMock.mock.calls.length - 1]
    expect(lastCall[0].skills).toEqual({ 'fast-talk': 25 })

    root.unmount()
  })
})
