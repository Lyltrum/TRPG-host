import { useMemo } from 'react'
import { Minus, Plus } from 'lucide-react'
import type { CharacterComputeResult, Ruleset } from 'trpg-sdk'
import { StepShell, StepSection } from '../components/StepShell'
import { PoolBar } from '../components/PoolBar'
import { SkillRow } from '../components/SkillRow'
import { buildSkillComputeMap, occupationStarSkillIds, totalPointsRemaining } from '../wizard-selectors'
import type { WizardAction, WizardState } from '../wizard-state'

/** 步 4 · 职业技能（重制设计 v2 §6-步骤4）：只列 ★ 技能 + 独立的信用评级卡片。 */
export function OccupationPointsStep({
  state,
  dispatch,
  ruleset,
  preview,
  pendingDelta,
}: {
  state: WizardState
  dispatch: (action: WizardAction) => void
  ruleset: Ruleset
  preview: CharacterComputeResult | null
  pendingDelta: number
}) {
  const selectedOcc = useMemo(() => ruleset.occupations.find((o) => o.id === state.occupationId) ?? null, [ruleset, state.occupationId])
  const skillComputeMap = useMemo(() => buildSkillComputeMap(preview), [preview])
  const remaining = totalPointsRemaining(preview, pendingDelta)

  const starIds = useMemo(
    () => occupationStarSkillIds(selectedOcc, state.slotPicks, preview),
    [selectedOcc, state.slotPicks, preview]
  )
  const starSkills = useMemo(
    () => ruleset.skills.filter((s) => starIds.has(s.id) && s.id !== 'credit-rating'),
    [ruleset, starIds]
  )

  const occBudget = preview?.occupationSkillPoints.budget ?? 0
  const occSpent = preview?.occupationSkillPoints.spent ?? 0

  const creditValue = state.skillAlloc['credit-rating'] ?? selectedOcc?.creditMin ?? 0

  const handleCreditChange = (delta: number) => {
    if (!selectedOcc) return
    const next = Math.max(selectedOcc.creditMin, Math.min(selectedOcc.creditMax, creditValue + delta))
    dispatch({ type: 'SET_SKILL_ALLOC', skillId: 'credit-rating', value: next })
  }

  const handleSkillChange = (skillId: string, delta: number) => {
    const current = state.skillAlloc[skillId] || 0
    dispatch({ type: 'SET_SKILL_ALLOC', skillId, value: Math.max(0, current + delta) })
  }

  const handleSkillSet = (skillId: string, value: number) => {
    dispatch({ type: 'SET_SKILL_ALLOC', skillId, value: Math.max(0, value) })
  }

  if (!selectedOcc) {
    return (
      <StepShell title="职业技能" lead="请先在上一步选择职业。">
        <StepSection title="尚未选择职业">
          <p className="text-[12px] text-text-muted">请先返回上一步选择职业。</p>
        </StepSection>
      </StepShell>
    )
  }

  return (
    <StepShell title="职业技能" lead={`${selectedOcc.name} 的职业技能点数（${selectedOcc.skillPointsFormula}）加点。`}>
      <PoolBar label={`职业技能点 (${selectedOcc.skillPointsFormula})`} spent={occSpent} budget={occBudget} />

      <StepSection title="信用评级 (Credit Rating) · 必填" accent tip="下限那部分算职业点，超出下限的部分算兴趣点（Chaosium 官方裁定）。">
        <div className="flex items-center justify-between mb-1">
          <span className="text-[11px] text-text-muted font-mono">
            范围 {selectedOcc.creditMin}–{selectedOcc.creditMax}
          </span>
        </div>
        <div className="flex items-center gap-3 mt-2">
          <button
            onClick={() => handleCreditChange(-1)}
            disabled={creditValue <= selectedOcc.creditMin}
            className="w-8 h-8 rounded-full bg-card border border-border-light text-text-muted flex items-center justify-center active:bg-panel active:scale-90 transition-all disabled:opacity-40 disabled:active:scale-100"
          >
            <Minus className="w-4 h-4" />
          </button>
          <div className="flex-1 text-center text-[20px] font-bold font-mono text-text-primary">{creditValue}</div>
          <button
            onClick={() => handleCreditChange(1)}
            disabled={creditValue >= selectedOcc.creditMax}
            className="w-8 h-8 rounded-full bg-card border border-border-light text-text-muted flex items-center justify-center active:bg-panel active:scale-90 transition-all disabled:opacity-40 disabled:active:scale-100"
          >
            <Plus className="w-4 h-4" />
          </button>
        </div>
      </StepSection>

      <div className="space-y-1.5">
        {starSkills.map((skill) => {
          const allocation = state.skillAlloc[skill.id] || 0
          const compute = skillComputeMap.get(skill.id)
          const base = compute?.base ?? (typeof skill.base === 'number' ? skill.base : 0)
          const cap = compute?.cap ?? null
          const slotted = state.slotPicks.some((picks) => picks.includes(skill.id)) || !!preview?.slotOccupiedSkillIds?.includes(skill.id)
          return (
            <SkillRow
              key={skill.id}
              skill={skill}
              base={base}
              cap={cap}
              allocation={allocation}
              onChange={(d) => handleSkillChange(skill.id, d)}
              onSetAllocation={(v) => handleSkillSet(skill.id, v)}
              maxPoints={allocation + remaining}
              minPoints={0}
              slotted={slotted}
            />
          )
        })}
      </div>

      <p className="text-[11px] text-text-dim px-1">职业点花完后，继续加点会自动占用兴趣点数（COC7 允许）。</p>
    </StepShell>
  )
}
