import { useEffect, useState } from 'react'
import type { CharacterComputeResult, Ruleset } from 'trpg-sdk'
import { useRoomStore } from '@/stores/room-store'
import { applyAgeAdjustment } from '@/services/character/character-api'
import { friendlyErrorMessage } from '@/services/api-client'
import { StepShell, StepSection } from '../components/StepShell'
import { AgeBandTable, describeAgeBand } from '../components/AgeBandTable'
import { AgeAdjustmentReport } from '../components/AgeAdjustmentReport'
import { ensureCharacterId, syncCurrentStateToBackend } from '../wizard-network'
import { buildSkillComputeMap, normalizeDerivedStats, pointBuyAttributes } from '../wizard-selectors'
import type { WizardAction, WizardState } from '../wizard-state'

/** 步 2 · 年龄（重制设计 v2 §6-步骤2）：必经步骤，"改了就生效"——失焦/回车
 * 提交新年龄后立即同步属性到后端并调用年龄调整端点，不再是可跳过的附加
 * 动作。 */
export function AgeStep({
  state,
  dispatch,
  ruleset,
  preview,
}: {
  state: WizardState
  dispatch: (action: WizardAction) => void
  ruleset: Ruleset
  preview: CharacterComputeResult | null
}) {
  const [ageInput, setAgeInput] = useState(String(state.age))
  const [applying, setApplying] = useState(false)
  const [error, setError] = useState('')
  const roomId = useRoomStore((s) => s.roomId)

  useEffect(() => {
    setAgeInput(String(state.age))
  }, [state.age])

  const attrs = pointBuyAttributes(ruleset)
  const attributesReady = attrs.length > 0 && attrs.every((a) => (state.attr[a.key] ?? 0) > 0)
  const range = ruleset.ageRange

  const applyNow = async (age: number) => {
    if (!roomId) {
      setError('房间信息丢失，请重新创建/加入房间')
      return
    }
    if (!attributesReady) return
    // 幂等：同一个年龄已经套用过就跳过，除非明确点了"重新套用"。
    if (state.ageApplied && state.ageAppliedFor === age) return
    setApplying(true)
    setError('')
    try {
      const characterId = await ensureCharacterId(roomId)
      const skillComputeMap = buildSkillComputeMap(preview)
      const derived = normalizeDerivedStats(preview?.derivedStats)
      const occupationName = ruleset.occupations.find((o) => o.id === state.occupationId)?.name ?? null
      // 年龄调整端点作用于后端已保存的 character.attributes，属性分配这一步
      // 全程只改本地 state，不先同步一次的话会套在陈旧属性上。
      await syncCurrentStateToBackend(
        roomId,
        characterId,
        state,
        { hp: derived.hp, san: derived.san, mp: derived.mp },
        skillComputeMap,
        occupationName
      )
      const result = await applyAgeAdjustment(roomId, characterId, age)
      dispatch({
        type: 'APPLY_AGE_SUCCESS',
        result,
        attrInputs: Object.fromEntries(attrs.map((a) => [a.key, String(result.attributesAfter[a.key] ?? '')])),
      })
    } catch (err) {
      setError(friendlyErrorMessage(err, '年龄调整失败'))
    } finally {
      setApplying(false)
    }
  }

  const commitAge = () => {
    const typed = parseInt(ageInput, 10)
    const clamped = Number.isNaN(typed)
      ? state.age
      : range
        ? Math.max(range.minValue, Math.min(range.maxValue, typed))
        : typed
    setAgeInput(String(clamped))
    dispatch({ type: 'SET_AGE', age: clamped })
    void applyNow(clamped)
  }

  const previewAge = (() => {
    const typed = parseInt(ageInput, 10)
    return Number.isNaN(typed) ? state.age : typed
  })()

  return (
    <StepShell title="年龄" lead="COC7 不同年龄段会对属性产生对应修正，改了年龄会立即重新套用。">
      {!attributesReady ? (
        <StepSection title="请先完成属性分配">
          <p className="text-[12px] text-text-muted">请先回到上一步完成属性分配，再进行年龄调整。</p>
        </StepSection>
      ) : (
        <>
          <StepSection title="当前年龄">
            <input
              type="number"
              min={range?.minValue}
              max={range?.maxValue}
              value={ageInput}
              onChange={(e) => setAgeInput(e.target.value)}
              onBlur={commitAge}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.currentTarget.blur()
                }
              }}
              className="w-full px-3.5 py-2.5 rounded-[6px] bg-input border border-border-light text-text-primary text-[15px] outline-none focus:border-brass"
            />
            <p className="text-[11px] text-text-muted mt-2">{describeAgeBand(previewAge)}</p>
          </StepSection>

          <StepSection title="年龄档对照表">
            <AgeBandTable age={previewAge} />
          </StepSection>

          {error && <p className="text-[11px] text-[#c04040]">{error}</p>}

          {state.ageApplied && (
            <button
              onClick={() => void applyNow(state.age)}
              disabled={applying}
              className="w-full py-2.5 rounded-sm text-sm font-semibold border border-border-mid bg-card text-text-body active:bg-panel transition-all disabled:opacity-60"
            >
              {applying ? '重新套用中…' : '重新套用（EDU 改进检定会重掷，可能变差）'}
            </button>
          )}
          {!state.ageApplied && applying && <p className="text-[12px] text-text-muted text-center">套用中…</p>}

          {state.ageResult && <AgeAdjustmentReport result={state.ageResult} />}
        </>
      )}
    </StepShell>
  )
}
