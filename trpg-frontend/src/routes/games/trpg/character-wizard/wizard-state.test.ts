import { describe, expect, it } from 'vitest'
import type { AgeAdjustmentResult } from 'trpg-sdk'
import { createInitialWizardState, wizardReducer } from './wizard-state'

// wizard-bugfix-round4.md 方案 A 的核心不变量：分配值（state.attr）和有效值
// （state.attrAfterAge）必须是两份独立的数据。
// - APPLY_AGE_SUCCESS 只写 attrAfterAge，绝不覆盖 attr——否则下一次套用年龄
//   调整会在已经修正过的值上再修一次（#18：属性被越扣越多）。
// - 任何会作废年龄状态的动作（SET_ATTR_VALUE 等）必须把 attrAfterAge 一并
//   清空——否则有效值会继续对应一份已经过期的分配值。
describe('wizardReducer：分配值与有效值的隔离（方案 A）', () => {
  const ageResult: AgeAdjustmentResult = {
    age: 45,
    ageLabel: '40-49 岁',
    attributesBefore: { STR: 60, CON: 60, DEX: 60, APP: 60, EDU: 60 },
    attributesAfter: { STR: 55, CON: 55, DEX: 55, APP: 55, EDU: 60 },
  }

  it('APPLY_AGE_SUCCESS 之后 attr 保持不变，attrAfterAge 等于 attributesAfter', () => {
    const before = { ...createInitialWizardState(), attr: { STR: 60, CON: 60, DEX: 60, APP: 60, EDU: 60 } }

    const after = wizardReducer(before, { type: 'APPLY_AGE_SUCCESS', result: ageResult })

    expect(after.attr).toEqual(before.attr)
    expect(after.attrAfterAge).toEqual(ageResult.attributesAfter)
    expect(after.ageApplied).toBe(true)
    expect(after.ageAppliedFor).toBe(45)
  })

  it('随后触发 invalidateAge（改属性）后，attrAfterAge 变回 null', () => {
    const withAgeApplied = wizardReducer(
      { ...createInitialWizardState(), attr: { STR: 60, CON: 60, DEX: 60, APP: 60, EDU: 60 } },
      { type: 'APPLY_AGE_SUCCESS', result: ageResult }
    )
    expect(withAgeApplied.attrAfterAge).not.toBeNull()

    const afterEdit = wizardReducer(withAgeApplied, { type: 'SET_ATTR_VALUE', key: 'STR', value: 65 })

    expect(afterEdit.attrAfterAge).toBeNull()
    expect(afterEdit.ageApplied).toBe(false)
    expect(afterEdit.ageAppliedFor).toBeNull()
  })
})
