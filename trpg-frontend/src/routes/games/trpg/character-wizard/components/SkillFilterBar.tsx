import { Search } from 'lucide-react'
import type { SkillFilter } from '../wizard-state'

const FILTER_OPTIONS: Array<{ key: SkillFilter; label: string }> = [
  { key: 'all', label: '全部技能' },
  { key: 'occupation', label: '只看本职 ★' },
  { key: 'allocated', label: '只看已加点' },
]

/** 兴趣技能步骤的工具条：筛选 + 分类（从 ruleset.skills[].category 动态
 * 生成，不硬编码分组）+ 搜索（重制设计 v2 §6-步骤5）。 */
export function SkillFilterBar({
  filter,
  onFilterChange,
  category,
  onCategoryChange,
  categories,
  search,
  onSearchChange,
}: {
  filter: SkillFilter
  onFilterChange: (f: SkillFilter) => void
  category: string | null
  onCategoryChange: (c: string | null) => void
  categories: string[]
  search: string
  onSearchChange: (s: string) => void
}) {
  return (
    <div className="space-y-2 mb-3">
      <div className="flex-1 relative">
        <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-text-dim" />
        <input
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder="搜索技能…"
          className="w-full pl-8 pr-3 py-2 text-[12px] rounded-[6px] bg-input border border-border-light outline-none focus:border-brass text-text-primary"
        />
      </div>
      <div className="flex gap-1.5 overflow-x-auto pb-0.5">
        {FILTER_OPTIONS.map((opt) => (
          <button
            key={opt.key}
            onClick={() => onFilterChange(opt.key)}
            className={`flex-shrink-0 px-2.5 py-1.5 text-[11px] font-semibold rounded-[6px] transition-all ${
              filter === opt.key ? 'bg-brass text-white' : 'bg-card border border-border-light text-text-muted'
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>
      <select
        value={category ?? ''}
        onChange={(e) => onCategoryChange(e.target.value || null)}
        className="w-full px-3 py-2 text-[12px] rounded-[6px] bg-input border border-border-light outline-none focus:border-brass text-text-primary"
      >
        <option value="">全部分类</option>
        {categories.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
    </div>
  )
}
