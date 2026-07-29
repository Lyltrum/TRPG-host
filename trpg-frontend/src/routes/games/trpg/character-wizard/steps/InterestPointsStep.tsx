import { useMemo } from 'react'
import type { CharacterComputeResult, Ruleset } from 'trpg-sdk'
import { StepShell, StepSection } from '../components/StepShell'
import { PoolBar } from '../components/PoolBar'
import { SkillFilterBar } from '../components/SkillFilterBar'
import { SkillRow } from '../components/SkillRow'
import { NON_ALLOCATABLE_SKILL_IDS, buildSkillComputeMap, occupationStarSkillIds, totalPointsRemaining } from '../wizard-selectors'
import type { WizardAction, WizardState } from '../wizard-state'

/** 步 5 · 兴趣技能（重制设计 v2 §6-步骤5）：全部技能（含职业技能，本职排前面
 * 带 ★）+ 筛选/分类/搜索。`skillAlloc` 是唯一真相，两池分账完全读 preview。 */
export function InterestPointsStep({
  state,
  dispatch,
  ruleset,
  preview,
  pendingDelta,
  previewError,
}: {
  state: WizardState
  dispatch: (action: WizardAction) => void
  ruleset: Ruleset
  preview: CharacterComputeResult | null
  pendingDelta: number
  previewError: string
}) {
  const selectedOcc = useMemo(() => ruleset.occupations.find((o) => o.id === state.occupationId) ?? null, [ruleset, state.occupationId])
  const skillComputeMap = useMemo(() => buildSkillComputeMap(preview), [preview])
  const remaining = totalPointsRemaining(preview, pendingDelta)

  const starIds = useMemo(
    () => occupationStarSkillIds(selectedOcc, state.slotPicks, preview),
    [selectedOcc, state.slotPicks, preview]
  )

  const categories = useMemo(() => Array.from(new Set(ruleset.skills.map((s) => s.category))).sort(), [ruleset])

  const visibleSkills = useMemo(() => {
    let list = ruleset.skills
    if (state.ui.skillFilter === 'occupation') list = list.filter((s) => starIds.has(s.id))
    if (state.ui.skillFilter === 'allocated') list = list.filter((s) => (state.skillAlloc[s.id] || 0) > 0)
    if (state.ui.skillCategory) list = list.filter((s) => s.category === state.ui.skillCategory)
    if (state.ui.skillSearch.trim()) {
      const q = state.ui.skillSearch.trim()
      list = list.filter((s) => s.name.includes(q))
    }
    return [...list].sort((a, b) => {
      const aStar = starIds.has(a.id) ? 1 : 0
      const bStar = starIds.has(b.id) ? 1 : 0
      if (aStar !== bStar) return bStar - aStar
      return a.name.localeCompare(b.name, 'zh')
    })
  }, [ruleset, state.ui, state.skillAlloc, starIds])

  const handleSkillChange = (skillId: string, delta: number) => {
    const current = state.skillAlloc[skillId] || 0
    dispatch({ type: 'SET_SKILL_ALLOC', skillId, value: Math.max(0, current + delta) })
  }

  const handleSkillSet = (skillId: string, value: number) => {
    dispatch({ type: 'SET_SKILL_ALLOC', skillId, value: Math.max(0, value) })
  }

  const intBudget = preview?.interestSkillPoints.budget ?? 0
  const intSpent = preview?.interestSkillPoints.spent ?? 0
  // preview 为空或技能视图为空时，预算/base/cap 全部是退化的默认值——给一句
  // 人能看懂的解释，不能让"预算显示 0/—"沉默地出现（重制设计 v2 bugfix #9）。
  const attrNotValidated = !preview || preview.skillView.length === 0

  return (
    <StepShell title="兴趣技能" lead="你有 INT×2 点可以随便加，也可以继续加在职业技能上。">
      {previewError && <p className="text-[11px] text-[#c04040] mb-2">{previewError}</p>}
      {!previewError && attrNotValidated && (
        <p className="text-[11px] text-[#8a6a2a] mb-2">属性还没通过校验，预算暂时无法计算。</p>
      )}
      <PoolBar
        label="兴趣点数 (INT×2)"
        spent={intSpent}
        budget={intBudget}
        remainingOverride={remaining}
      />

      <StepSection title="筛选">
        <SkillFilterBar
          filter={state.ui.skillFilter}
          onFilterChange={(f) => dispatch({ type: 'SET_UI', patch: { skillFilter: f } })}
          category={state.ui.skillCategory}
          onCategoryChange={(c) => dispatch({ type: 'SET_UI', patch: { skillCategory: c } })}
          categories={categories}
          search={state.ui.skillSearch}
          onSearchChange={(s) => dispatch({ type: 'SET_UI', patch: { skillSearch: s } })}
        />
      </StepSection>

      <div className="space-y-1.5">
        {visibleSkills.map((skill) => {
          const allocation = state.skillAlloc[skill.id] || 0
          const compute = skillComputeMap.get(skill.id)
          const base = compute?.base ?? (typeof skill.base === 'number' ? skill.base : 0)
          const cap = compute?.cap ?? null
          const isStar = starIds.has(skill.id)
          const disabled = NON_ALLOCATABLE_SKILL_IDS.has(skill.id)
          return (
            <SkillRow
              key={skill.id}
              skill={{ ...skill, name: isStar ? `★ ${skill.name}` : skill.name }}
              base={base}
              cap={cap}
              allocation={allocation}
              onChange={(d) => handleSkillChange(skill.id, d)}
              onSetAllocation={(v) => handleSkillSet(skill.id, v)}
              maxPoints={allocation + remaining}
              minPoints={0}
              disabled={disabled}
              disabledReason="建卡阶段不可加点"
            />
          )
        })}
        {visibleSkills.length === 0 && <p className="text-center py-8 text-text-muted text-sm">没有符合条件的技能</p>}
      </div>
    </StepShell>
  )
}
