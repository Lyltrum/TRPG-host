import { useEffect, useRef, useState } from 'react'
import type { CharacterComputeResult, Ruleset } from 'trpg-sdk'
import { friendlyErrorMessage } from '@/services/api-client'
import { previewCharacter } from '@/services/character/ruleset-api'
import { buildSkillComputeMap, sumValues } from './wizard-selectors'
import { buildSkillsPayload } from './wizard-network'
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

  // 属性还没配齐（尤其是幸运没掷）之前，这份草稿本来就注定不完整，对它跑
  // 规则校验没有意义——不发请求，也不该产出任何"问题"提示给用户看（#11）。
  const attrsReady = !!ruleset && ruleset.attributes.every((a) => typeof state.attr[a.key] === 'number')

  useEffect(() => {
    if (!ruleset || !attrsReady) {
      // 让飞行中的旧请求作废，避免它在属性配齐前就把 preview 设回一个陈旧值。
      previewGenRef.current += 1
      setPreview(null)
      setPreviewError('')
      return
    }
    const timer = setTimeout(() => {
      const gen = ++previewGenRef.current
      const allocTotalAtRequest = sumValues(state.skillAlloc)
      // state.skillAlloc 存的是"加了多少点"的增量，后端 skills 字段要的是
      // 最终绝对值——用 hook 自己上一次成功响应的 preview 构造 base 映射转换
      // （核心 bug 修复：此前这里直接把增量当绝对值发出去）。
      const skillComputeMap = buildSkillComputeMap(preview)
      const skillsPayload = buildSkillsPayload(state.skillAlloc, skillComputeMap)
      previewCharacter({
        attributes: state.attr,
        occupationId: state.occupationId,
        skills: skillsPayload,
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    ruleset,
    attrsReady,
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
