import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { SkillComputeView } from 'trpg-sdk'
import { saveCharacter } from '@/services/character/character-api'
import { syncCurrentStateToBackend } from './wizard-network'
import { createInitialWizardState, type WizardState } from './wizard-state'

// 真人实测 exec/12 #31：编辑一张已套过年龄修正的角色卡，一进技能页就凭空
// 超支 8 点。根因是这条同步路径把「有效值」字段也写成了分配值，等于把年龄
// 修正从已保存的卡上抹掉 → 职业点预算按更低的 EDU 重算。
// 首次建卡看不出问题（那时两者本来就相等），所以两轮 review 都没发现。
vi.mock('@/services/character/character-api', () => ({
  saveCharacter: vi.fn(async () => undefined),
  createCharacterDraft: vi.fn(async () => 'char-1'),
}))

const saveCharacterMock = vi.mocked(saveCharacter)

const skillComputeMap = new Map<string, SkillComputeView>()

function stateWithAgeAdjustment(): WizardState {
  const base = createInitialWizardState()
  return {
    ...base,
    age: 30,
    // 分配值：玩家自己分的
    attr: { STR: 60, CON: 60, POW: 60, DEX: 55, APP: 55, SIZ: 55, INT: 55, EDU: 55, LUCK: 65 },
    // 有效值：年龄修正把 EDU 提到了 59（EDU 增强检定）
    attrAfterAge: { STR: 60, CON: 60, POW: 60, DEX: 55, APP: 55, SIZ: 55, INT: 55, EDU: 59, LUCK: 65 },
  }
}

describe('syncCurrentStateToBackend', () => {
  beforeEach(() => vi.clearAllMocks())

  it('🔴 attributes 存有效值、allocatedAttributes 存分配值——两份数据语义不同', async () => {
    await syncCurrentStateToBackend(
      'room-1',
      'char-1',
      stateWithAgeAdjustment(),
      { hp: 12, san: 60, mp: 12 },
      skillComputeMap,
      '杂技演员'
    )

    const payload = saveCharacterMock.mock.calls[0][2]
    // 有效值：年龄修正后的 EDU，预算/衍生值/技能 base 都基于它
    expect(payload.attr.EDU).toBe(59)
    // 分配值：apply-age-adjustment 必须基于它重算才幂等
    expect(payload.allocatedAttributes?.EDU).toBe(55)
  })

  it('还没做年龄调整时两者相同（首次建卡——正因如此这个 bug 藏了两轮）', async () => {
    const state = { ...stateWithAgeAdjustment(), attrAfterAge: null }
    await syncCurrentStateToBackend(
      'room-1',
      'char-1',
      state,
      { hp: 12, san: 60, mp: 12 },
      skillComputeMap,
      '杂技演员'
    )
    const payload = saveCharacterMock.mock.calls[0][2]
    expect(payload.attr.EDU).toBe(55)
    expect(payload.allocatedAttributes?.EDU).toBe(55)
  })
})
