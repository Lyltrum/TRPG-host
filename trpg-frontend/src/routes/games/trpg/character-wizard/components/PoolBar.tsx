/** 预算条：属性池 / 职业点 / 兴趣点三处复用。"已花"永远来自调用方传入的
 * 权威数字（preview 或 ruleset 驱动的静态配置），这个组件本身不做任何
 * 规则计算，只负责渲染。 */
export function PoolBar({
  label,
  spent,
  budget,
  exactMatchRequired,
  remainingOverride,
}: {
  label: string
  spent: number
  budget: number
  /** 掷点池模式下总和必须精确等于总值，而不是"不超过即可"——这里决定
   * 剩余 !== 0 时是否也算超支样式（§6-步骤1，两种模式校验口径不同）。 */
  exactMatchRequired?: boolean
  /** 职业/兴趣技能两步共用一个"剩余点数"口径（issue #21）：默认按
   * `budget - spent` 算的是"这一个池子自己还剩多少"，但底部确认按钮显示
   * 的是两池合计——同一句"还剩 X 点"文案两种口径会让人以为数字对不上。
   * 传入这个值后，右侧的"还剩 X 点"文案和超支样式改用它，不再用本池
   * 自己的 budget-spent；进度条的填充比例仍然按本池自己的 spent/budget
   * 画（展示"这个池子花了多少"依然有意义），不受这个覆盖值影响。 */
  remainingOverride?: number
}) {
  const remaining = remainingOverride ?? budget - spent
  const pct = budget > 0 ? Math.min(100, (spent / budget) * 100) : 0
  const isOver = remaining < 0 || (exactMatchRequired && remaining !== 0)
  return (
    // 方头刻度量表，不是圆角进度条（同角色卡上的技能条）。超支时整条变锈红。
    <div>
      <div className="flex items-baseline gap-1.5 mb-1 text-[10.5px] text-ink-soft">
        <span>{label}</span>
        <span className={`font-mono font-bold ${isOver ? 'text-rust-dark' : 'text-ink'}`}>
          {spent}
          <span className="text-ink-soft font-normal">/{budget || '—'}</span>
        </span>
        <span className={`ml-auto ${remaining < 0 ? 'text-rust-dark font-bold' : ''}`}>
          {remaining < 0 ? `多花了 ${-remaining} 点` : `还剩 ${remaining} 点`}
        </span>
      </div>
      <div className="gauge h-[10px]">
        <div
          className={`absolute inset-y-0 left-0 transition-all duration-300 ${
            isOver ? 'bg-rust-dark' : 'bg-brass-dark'
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}
