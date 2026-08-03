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
    // 索引卡行：选中的那张加粗黄铜边（表单里"划勾"的那一行）
    <button
      onClick={onSelect}
      className={`w-full text-left px-2.5 py-1.5 border transition-all ${
        selected ? 'border-[1.5px] border-brass-dark bg-white/30' : 'border-ink/28 bg-white/12 active:bg-white/25'
      }`}
    >
      <div className="text-[12px] font-semibold text-ink">{occ.name}</div>
      <div className="text-[10.5px] text-ink-soft font-mono mt-0.5">
        {occ.skillPointsFormula} · 信用 {occ.creditMin}–{occ.creditMax}
      </div>
    </button>
  )
}
