import { useEffect, useMemo, useState } from 'react'
import { Search } from 'lucide-react'
import type { CharacterComputeResult, Ruleset } from 'trpg-sdk'
import { StepShell, StepSection } from '../components/StepShell'
import { OccupationListItem } from '../components/OccupationListItem'
import { ChoiceSlotPicker } from '../components/ChoiceSlotPicker'
import type { WizardAction, WizardState } from '../wizard-state'

/** 职业技能点公式里的 `MAX(DEX, APP)` 这类二选一——系统自动取对玩家有利的
 * 一项，不需要下拉选择（重制设计 v2 §9 不做清单第 1 条）。这里只做展示：
 * 解析出候选属性键，标出当前取的是哪一项。 */
function describeMaxFormula(formula: string, attr: Record<string, number>): string | null {
  const match = formula.match(/MAX\(([^)]+)\)/)
  if (!match) return null
  const keys = match[1].split(',').map((s) => s.trim())
  if (keys.length < 2) return null
  let bestKey = keys[0]
  for (const k of keys) {
    if ((attr[k] ?? 0) > (attr[bestKey] ?? 0)) bestKey = k
  }
  return `系统按对你有利的一项计算，当前取 ${bestKey}（${attr[bestKey] ?? 0}）`
}

/** 步 3 · 选职业（重制设计 v2 §6-步骤3）：搜索 + 紧凑列表 + 详情面板 + 自选槽区。 */
export function OccupationStep({
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
  const [search, setSearch] = useState(state.ui.occSearch)
  const [category, setCategory] = useState<string | null>(null)

  // 分类 tab 从 ruleset.occupations 已有的 category 字段去重生成，不硬编码
  // 12 类目名单——分类调整时前端不用跟着改（重制设计 v2 §6-步骤3）。
  const categories = useMemo(
    () => Array.from(new Set(ruleset.occupations.map((o) => o.category))).sort(),
    [ruleset]
  )

  const selectedOcc = useMemo(
    () => ruleset.occupations.find((o) => o.id === state.occupationId) ?? null,
    [ruleset, state.occupationId]
  )

  // 编辑已有卡水合后 slotPicks 可能还没被初始化成这个职业的槽形状（水合
  // 不填 slotPicks，见 useWizardHydration 注释）——这里补一次，只在形状
  // 不匹配时才 dispatch，不覆盖玩家已经做出的选择。
  useEffect(() => {
    if (!selectedOcc) return
    const expectedLen = selectedOcc.choiceSlots?.length ?? 0
    if (state.slotPicks.length === expectedLen) return
    dispatch({
      type: 'SET_OCCUPATION',
      occupationId: selectedOcc.id,
      slotCounts: (selectedOcc.choiceSlots ?? []).map((s) => s.count),
      creditMin: selectedOcc.creditMin,
      creditMax: selectedOcc.creditMax,
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedOcc, state.slotPicks.length])

  const filtered = useMemo(() => {
    const q = search.trim()
    let list = ruleset.occupations
    if (category) {
      list = list.filter((o) => o.category === category)
    }
    if (q) {
      list = list.filter((o) => o.name.includes(q) || o.description.includes(q))
    }
    return list
  }, [ruleset, search, category])

  // 不再截断——分类+搜索组合后每类最多几十个，列表容器本身已经有滚动
  // （重制设计 v2 §6-步骤3：任何一个职业都必须能被精确找到，不能因为排序
  // 靠后而永久不可达）。
  const showAllHint = category == null && !search.trim()

  const handleSelect = (occId: number) => {
    const occ = ruleset.occupations.find((o) => o.id === occId)
    if (!occ) return
    dispatch({
      type: 'SET_OCCUPATION',
      occupationId: occ.id,
      slotCounts: (occ.choiceSlots ?? []).map((s) => s.count),
      creditMin: occ.creditMin,
      creditMax: occ.creditMax,
    })
  }

  const maxNote = selectedOcc ? describeMaxFormula(selectedOcc.skillPointsFormula, state.attr) : null
  const budget = preview?.occupationSkillPoints.budget

  // 跨槽去重：已经被任意槽位选中的技能，在其余下拉里禁用——每个下拉自己的
  // 当前值仍然可选（ChoiceSlotPicker 内部按 `o.id === pick` 放行自己），
  // 所以这里不需要按 slotIndex/pickIndex 排除，全量集合即可。
  const disabledElsewhere = useMemo(() => {
    const disabled = new Set<string>()
    state.slotPicks.forEach((picks) => {
      picks.forEach((skillId) => {
        if (skillId) disabled.add(skillId)
      })
    })
    return disabled
  }, [state.slotPicks])

  return (
    <StepShell
      lead="搜索并选择一个职业；自选槽可以先留空，后续在技能加点步骤里直接给对应技能加点即可自动占槽。"
    >
      <StepSection title="搜索职业">
        <div className="relative mb-3">
          <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-text-dim" />
          <input
            value={search}
            onChange={(e) => {
              setSearch(e.target.value)
              dispatch({ type: 'SET_UI', patch: { occSearch: e.target.value } })
            }}
            placeholder="搜索职业名称…"
            className="wz-field w-full pl-8 pr-3 py-2 text-[12px]"
          />
        </div>
        <div className="flex gap-1.5 overflow-x-auto pb-1.5 mb-1">
          <button
            onClick={() => setCategory(null)}
            className={`flex-shrink-0 px-2.5 py-1 text-[10.5px] font-semibold transition-all ${
              category === null ? 'bg-brass-dark text-dossier border border-brass-dark' : 'border border-ink/30 bg-white/20 text-ink-soft'
            }`}
          >
            全部
          </button>
          {categories.map((c) => (
            <button
              key={c}
              onClick={() => setCategory(c)}
              className={`flex-shrink-0 px-2.5 py-1 text-[10.5px] font-semibold transition-all ${
                category === c ? 'bg-brass-dark text-dossier border border-brass-dark' : 'border border-ink/30 bg-white/20 text-ink-soft'
              }`}
            >
              {c}
            </button>
          ))}
        </div>
        <div className="space-y-1.5 max-h-[360px] overflow-y-auto pr-0.5">
          {filtered.map((occ) => (
            <OccupationListItem key={occ.id} occ={occ} selected={state.occupationId === occ.id} onSelect={() => handleSelect(occ.id)} />
          ))}
        </div>
        {showAllHint && (
          <p className="text-[10.5px] text-ink-soft text-center mt-1.5">
            已显示全部 {filtered.length} 项，试试按分类或搜索缩小范围
          </p>
        )}
      </StepSection>

      {selectedOcc && (
        <StepSection title={`${selectedOcc.name} · 详情`} accent>
          <p className="text-[12px] text-text-body mb-2">
            信用评级 {selectedOcc.creditMin}–{selectedOcc.creditMax}
            {budget != null && <> · 下一步大约有 {budget} 点职业技能可加</>}
          </p>
          <p className="text-[10.5px] text-ink-soft font-mono mb-1">技能点公式 {selectedOcc.skillPointsFormula}</p>
          {maxNote && <p className="text-[10.5px] text-ink-soft mb-2">{maxNote}</p>}
          <div className="flex flex-wrap gap-1.5 mt-2">
            {selectedOcc.skillIds.map((id) => {
              const skill = ruleset.skills.find((s) => s.id === id)
              return (
                <span key={id} className="px-2 py-0.5 text-[10.5px] border border-ink/30 bg-white/20 text-ink">
                  {skill?.name ?? id}
                </span>
              )
            })}
          </div>
        </StepSection>
      )}

      {selectedOcc && (selectedOcc.choiceSlots ?? []).length > 0 && (
        <StepSection title="自选技能槽">
          <div className="space-y-2.5">
            {(selectedOcc.choiceSlots ?? []).map((slot, slotIndex) => (
              <ChoiceSlotPicker
                key={slotIndex}
                slot={slot}
                picks={state.slotPicks[slotIndex] ?? Array(slot.count).fill('')}
                allSkills={ruleset.skills}
                disabledElsewhere={disabledElsewhere}
                onPick={(pickIndex, skillId) => dispatch({ type: 'SET_SLOT_PICK', slotIndex, pickIndex, skillId })}
              />
            ))}
          </div>
        </StepSection>
      )}
    </StepShell>
  )
}
