import { useEffect, useRef, useState } from 'react'
import type { CharacterComputeResult, Ruleset } from 'trpg-sdk'
import { friendlyErrorMessage } from '@/services/api-client'
import { previewCharacter } from '@/services/character/ruleset-api'
import { sumValues } from './wizard-selectors'
import type { WizardState } from './wizard-state'

/**
 * 建卡计算预览（issue #84 S2 previewCharacter，路线乙的唯一接缝）：衍生值/
 * 技能点预算/每个技能的 base·cap/校验报告 + 两条 bar 的"已花"全部来自这里，
 * 属性/职业/技能/年龄任一变化都会（防抖 400ms）重新预演。
 *
 * `pendingDelta`：加点手感依赖同步反馈，但预算数字要等 preview 防抖+网络
 * 往返才更新——这里用"当前 skillAlloc 总和 − 上一次成功确认时发出请求那刻
 * 的总和"算出还没被 preview 确认的净加点，只影响"还能不能继续加"的判断
 * （见 wizard-selectors.totalPointsRemaining），不影响两条 bar 本身的显示值。
 */
export function useWizardPreview(ruleset: Ruleset | null, state: WizardState) {
  const [preview, setPreview] = useState<CharacterComputeResult | null>(null)
  const [previewError, setPreviewError] = useState('')
  const [confirmedAllocTotal, setConfirmedAllocTotal] = useState(0)
  const previewGenRef = useRef(0)

  useEffect(() => {
    if (!ruleset) return
    const timer = setTimeout(() => {
      const gen = ++previewGenRef.current
      const allocTotalAtRequest = sumValues(state.skillAlloc)
      previewCharacter({
        attributes: state.attr,
        occupationId: state.occupationId,
        skills: state.skillAlloc,
        age: state.age,
        generationMethod: state.generationMethod,
        attributePoolTotal: state.attributePoolTotal,
      })
        .then((result) => {
          if (gen !== previewGenRef.current) return
          setPreview(result)
          setPreviewError('')
          setConfirmedAllocTotal(allocTotalAtRequest)
        })
        .catch((err) => {
          if (gen !== previewGenRef.current) return
          setPreviewError(friendlyErrorMessage(err, '规则计算失败'))
        })
    }, 400)
    return () => clearTimeout(timer)
  }, [
    ruleset,
    state.attr,
    state.occupationId,
    state.skillAlloc,
    state.age,
    state.generationMethod,
    state.attributePoolTotal,
  ])

  const pendingDelta = Math.max(0, sumValues(state.skillAlloc) - confirmedAllocTotal)

  return { preview, previewError, pendingDelta }
}
