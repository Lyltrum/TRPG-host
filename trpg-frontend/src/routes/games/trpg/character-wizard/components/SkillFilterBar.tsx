import { Search } from 'lucide-react'
import type { SkillFilter } from '../wizard-state'

const FILTER_OPTIONS: Array<{ key: SkillFilter; label: string }> = [
  { key: 'all', label: '全部技能' },
  { key: 'occupation', label: '只看本职 ★' },
  { key: 'allocated', label: '只看已加点' },
]

/** 技能分类的中文展示映射——`categories` prop 传入的是后端
 * `SkillSpec.category` 的英文机器可读标识，这里只做展示层翻译，不改
 * 传值本身（`value`/`onCategoryChange`/`state.ui.skillCategory` 仍然用
 * 英文原值比较）。映射表里没有的值兜底显示原文，防止以后后端新增分类
 * 时这里直接显示空白。 */
const SKILL_CATEGORY_LABELS: Record<string, string> = {
  combat: '战斗',
  knowledge: '知识',
  language: '语言',
  perception: '感知',
  physical: '体能',
  social: '社交',
  technical: '技术',
}

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
          className="wz-field w-full pl-8 pr-3 py-2 text-[12px]"
        />
      </div>
      <div className="flex gap-1.5 overflow-x-auto pb-0.5">
        {FILTER_OPTIONS.map((opt) => (
          <button
            key={opt.key}
            onClick={() => onFilterChange(opt.key)}
            className={`flex-shrink-0 px-2.5 py-1 text-[10.5px] font-semibold transition-all ${
              filter === opt.key ? 'bg-brass-dark text-dossier border border-brass-dark' : 'border border-ink/30 bg-white/20 text-ink-soft'
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>
      <select
        value={category ?? ''}
        onChange={(e) => onCategoryChange(e.target.value || null)}
        className="wz-field w-full px-3 py-2 text-[12px]"
      >
        <option value="">全部分类</option>
        {categories.map((c) => (
          <option key={c} value={c}>
            {SKILL_CATEGORY_LABELS[c] ?? c}
          </option>
        ))}
      </select>
    </div>
  )
}
