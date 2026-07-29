import { useRef, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import type { SkillChoiceSlot, SkillSpec } from '@/data/types'

/** 下拉菜单的 `max-h`（见下方 `max-h-[220px]`），用作判断触发按钮下方
 * 空间是否足够展开的阈值——不需要精确测量实际渲染高度，够用即可。 */
const MENU_MAX_HEIGHT = 220

interface SkillOption {
  id: string
  label: string
}

/** 单个下拉/组合框：候选集 ≤15 项时用原生 `<select>`，否则给一个可搜索的
 * 选择器（79 项技能全表下拉太长，见重制设计 v2 §6-步骤3）。 */
function SkillCombobox({
  options,
  value,
  onChange,
  placeholder,
  searchable,
}: {
  options: SkillOption[]
  value: string
  onChange: (id: string) => void
  placeholder: string
  searchable: boolean
}) {
  const [open, setOpen] = useState(false)
  const [openUpward, setOpenUpward] = useState(false)
  const [query, setQuery] = useState('')
  const selected = options.find((o) => o.id === value)
  const buttonRef = useRef<HTMLButtonElement>(null)

  const handleToggle = () => {
    if (open) {
      setOpen(false)
      return
    }
    const rect = buttonRef.current?.getBoundingClientRect()
    const spaceBelow = rect ? window.innerHeight - rect.bottom : Infinity
    setOpenUpward(spaceBelow < MENU_MAX_HEIGHT)
    setOpen(true)
  }

  if (!searchable) {
    return (
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full px-3 py-2 text-[13px] rounded-[6px] bg-input border border-border-light outline-none focus:border-brass text-text-primary"
      >
        <option value="">{placeholder}</option>
        {options.map((o) => (
          <option key={o.id} value={o.id}>
            {o.label}
          </option>
        ))}
      </select>
    )
  }

  const filtered = query.trim() ? options.filter((o) => o.label.includes(query.trim())) : options

  return (
    <div className="relative">
      <button
        ref={buttonRef}
        type="button"
        onClick={handleToggle}
        className="w-full flex items-center justify-between px-3 py-2 text-[13px] rounded-[6px] bg-input border border-border-light text-left text-text-primary"
      >
        <span className={selected ? '' : 'text-text-dim'}>{selected?.label ?? placeholder}</span>
        <ChevronDown className="w-3.5 h-3.5 text-text-dim flex-shrink-0" />
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div
            className={`absolute left-0 right-0 z-20 bg-card border border-border-light rounded-md shadow-lg max-h-[220px] overflow-y-auto ${
              openUpward ? 'bottom-full mb-1' : 'top-full mt-1'
            }`}
          >
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索技能…"
              className="w-full px-3 py-2 text-[12px] border-b border-border-light outline-none bg-transparent text-text-primary sticky top-0 bg-card"
            />
            {filtered.map((o) => (
              <button
                key={o.id}
                type="button"
                onClick={() => {
                  onChange(o.id)
                  setOpen(false)
                  setQuery('')
                }}
                className={`w-full text-left px-3 py-2 text-[12px] hover:bg-panel ${
                  o.id === value ? 'text-brass-dark font-semibold' : 'text-text-primary'
                }`}
              >
                {o.label}
              </button>
            ))}
            {filtered.length === 0 && <div className="px-3 py-2 text-[12px] text-text-dim">没有匹配的技能</div>}
          </div>
        </>
      )}
    </div>
  )
}

/** 一个自选槽：`count` 个下拉，选项只来自 `candidateSkillIds`（null 时给
 * 全技能可搜索选择器）。跨槽去重由调用方传入 `disabledElsewhere`
 * （重制设计 v2 §6-步骤3）。 */
export function ChoiceSlotPicker({
  slot,
  picks,
  allSkills,
  disabledElsewhere,
  onPick,
}: {
  slot: SkillChoiceSlot
  picks: string[]
  allSkills: SkillSpec[]
  disabledElsewhere: Set<string>
  onPick: (pickIndex: number, skillId: string) => void
}) {
  const skillById = new Map(allSkills.map((s) => [s.id, s]))
  const candidateIds = slot.candidateSkillIds ?? allSkills.map((s) => s.id)
  const pool: SkillOption[] = candidateIds.map((id) => ({ id, label: skillById.get(id)?.name ?? id }))
  const searchable = slot.candidateSkillIds == null

  return (
    <div className="bg-input border border-border-light rounded-[6px] p-3">
      <div className="text-[12px] font-medium text-text-primary mb-2">{slot.label}</div>
      <div className="space-y-1.5">
        {picks.map((pick, pickIndex) => {
          const options = pool.filter((o) => o.id === pick || !disabledElsewhere.has(o.id))
          return (
            <div key={pickIndex}>
              <SkillCombobox
                options={options}
                value={pick}
                onChange={(id) => onPick(pickIndex, id)}
                placeholder={picks.length > 1 ? `— 请选择第 ${pickIndex + 1} 项 —` : '— 请选择 —'}
                searchable={searchable}
              />
            </div>
          )
        })}
      </div>
    </div>
  )
}
