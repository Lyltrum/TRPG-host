import type { OccupationSpec } from '@/data/types'

/** 紧凑的两行职业列表行：名称 / 公式·信用区间，无图标无描述
 * （重制设计 v2 §6-步骤3，取代原来只覆盖 30/229 个职业的图标网格）。 */
export function OccupationListItem({
  occ,
  selected,
  onSelect,
}: {
  occ: OccupationSpec
  selected: boolean
  onSelect: () => void
}) {
  return (
    <button
      onClick={onSelect}
      className={`w-full text-left px-3 py-2 rounded-[6px] border transition-all ${
        selected ? 'border-brass bg-[#fdfaf4] shadow-[0_0_0_2px_rgba(184,151,106,0.15)]' : 'border-border-light bg-input active:bg-panel'
      }`}
    >
      <div className="text-[13px] font-semibold text-text-primary">{occ.name}</div>
      <div className="text-[10px] text-text-dim font-mono mt-0.5">
        {occ.skillPointsFormula} · 信用 {occ.creditMin}–{occ.creditMax}
      </div>
    </button>
  )
}
