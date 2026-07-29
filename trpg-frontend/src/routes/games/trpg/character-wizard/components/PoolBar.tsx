/** 预算条：属性池 / 职业点 / 兴趣点三处复用。"已花"永远来自调用方传入的
 * 权威数字（preview 或 ruleset 驱动的静态配置），这个组件本身不做任何
 * 规则计算，只负责渲染。 */
export function PoolBar({
  label,
  spent,
  budget,
  exactMatchRequired,
}: {
  label: string
  spent: number
  budget: number
  /** 掷点池模式下总和必须精确等于总值，而不是"不超过即可"——这里决定
   * 剩余 !== 0 时是否也算超支样式（§6-步骤1，两种模式校验口径不同）。 */
  exactMatchRequired?: boolean
}) {
  const remaining = budget - spent
  const pct = budget > 0 ? Math.min(100, (spent / budget) * 100) : 0
  const isOver = remaining < 0 || (exactMatchRequired && remaining !== 0)
  return (
    <div className="bg-panel rounded-md px-3.5 py-2 flex items-center gap-3">
      <div className="flex-1">
        <div className="flex items-center justify-between mb-1">
          <span className="text-[11px] font-medium text-text-muted">{label}</span>
          <span className={`text-[12px] font-bold font-mono ${isOver ? 'text-[#c04040]' : 'text-text-primary'}`}>
            {spent}
            <span className="text-text-dim font-normal">/{budget || '—'}</span>
          </span>
        </div>
        <div className="h-1.5 rounded-full bg-border-light overflow-hidden">
          <div
            className={`h-full rounded-full transition-all duration-300 ${isOver ? 'bg-[#c04040]' : 'bg-brass'}`}
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>
      <span className={`text-[10px] whitespace-nowrap ${remaining < 0 ? 'text-[#c04040] font-semibold' : 'text-text-dim'}`}>
        {remaining < 0 ? `多花了 ${-remaining} 点` : `还剩 ${remaining} 点`}
      </span>
    </div>
  )
}
