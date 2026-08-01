import { useEffect, useState } from 'react'
import { Minus, Plus } from 'lucide-react'
import type { SkillSpec } from '@/data/types'

/**
 * 单个技能行（从 CharacterPage.tsx 原有实现提取，重制设计 v2 §7）。
 *
 * 两处必须原样保留：
 * - `inputValue` 字符串镜像 + `onBlur` 提交——允许先清空再打字，不在每次
 *   按键就夹值；
 * - `maxPoints` 口径——调用方必须传"这个技能当前分配 + 两池合计剩余"，
 *   不能分别按职业/兴趣池相加（PR #119 review 修过：分开算会在同一个值上
 *   代数抵消，导致连续快点时门槛形同虚设）。
 *
 * 重制后不再有 `otherPoolPoints`（旧架构里"另一个池已经加的、这里只读"的
 * 参数）——`skillAlloc` 现在是唯一真相，`allocation` 就是这个技能的总加点。
 */
export function SkillRow({
  skill,
  base,
  cap,
  allocation,
  onChange,
  onSetAllocation,
  maxPoints,
  minPoints,
  slotted,
  disabled,
  disabledReason,
}: {
  skill: SkillSpec
  base: number
  // 后端还没返回权威计算结果时是 null——此时不允许加点，而不是前端自己编一个
  // 上限。
  cap: number | null
  allocation: number
  onChange: (delta: number) => void
  onSetAllocation: (allocation: number) => void
  maxPoints: number
  minPoints: number
  /** 这项技能占了一个职业自选槽（§11 风险 1：徽标一律以后端权威记账为准）。 */
  slotted?: boolean
  /** 建卡阶段不可分配（如克苏鲁神话）。 */
  disabled?: boolean
  disabledReason?: string
}) {
  const current = base + allocation
  const canAdd = !disabled && cap !== null && allocation < maxPoints && current < cap
  const canSub = !disabled && allocation > minPoints

  // ±5 快捷键（真人实测：一点一点加太慢，属性步骤早就有 ±5，技能步骤没有）。
  // 🔴 走 onSetAllocation 而不是 onChange(±5)：调用方的 onChange 只夹了下界，
  // 上界靠按钮 disabled 拦——步长变成 5 之后"差 3 点到上限"就会一脚踩过。
  // 这里按 [minPoints, min(maxPoints, cap-base)] 夹住，够不到 5 点就加到顶。
  const adjustBy5 = (delta: number) => {
    if (disabled || cap === null) return
    const maxAlloc = Math.min(maxPoints, cap - base)
    const next = Math.max(minPoints, Math.min(maxAlloc, allocation + delta))
    if (next !== allocation) onSetAllocation(next)
  }
  const step5Class =
    'w-7 h-6 rounded-full flex items-center justify-center text-[10px] font-mono font-semibold transition-all'

  const [inputValue, setInputValue] = useState(String(current))
  useEffect(() => {
    setInputValue(String(current))
  }, [current])

  const commitInput = () => {
    if (disabled) return
    const typed = parseInt(inputValue, 10)
    if (Number.isNaN(typed)) {
      setInputValue(String(current))
      return
    }
    if (cap === null) {
      setInputValue(String(current))
      return
    }
    const maxAllocByCap = Math.min(maxPoints, cap - base)
    const newAlloc = Math.max(minPoints, Math.min(maxAllocByCap, typed - base))
    onSetAllocation(newAlloc)
    setInputValue(String(base + newAlloc))
  }

  return (
    <div
      className={`flex items-center gap-2.5 px-3 py-2 bg-input border border-border-light rounded-[6px] ${
        disabled ? 'opacity-60' : ''
      }`}
    >
      <div className="flex-1 min-w-0">
        <div className="text-[13px] font-medium text-text-primary flex items-center gap-1.5">
          <span className="truncate">{skill.name}</span>
          {slotted && (
            <span className="flex-shrink-0 px-1 py-0 text-[9px] font-semibold rounded bg-brass/15 text-brass-dark">
              槽
            </span>
          )}
        </div>
        <div className="text-[10px] text-text-dim font-mono">{disabled ? disabledReason ?? '建卡阶段不可加点' : skill.nameEn}</div>
      </div>
      <div className="text-[10px] text-text-muted font-mono min-w-[32px] text-center">{base}%</div>
      <button
        onClick={() => adjustBy5(-5)}
        className={`${step5Class} ${
          canSub
            ? 'bg-card border border-border-light text-text-muted active:bg-panel active:scale-90'
            : 'bg-transparent text-border-light cursor-not-allowed'
        }`}
        disabled={!canSub}
      >
        −5
      </button>
      <button
        onClick={() => onChange(-1)}
        className={`w-6 h-6 rounded-full flex items-center justify-center transition-all ${
          canSub ? 'bg-card border border-border-light text-text-muted active:bg-panel active:scale-90' : 'bg-transparent text-border-light cursor-not-allowed'
        }`}
        disabled={!canSub}
      >
        <Minus className="w-3 h-3" />
      </button>
      <input
        type="number"
        inputMode="numeric"
        value={inputValue}
        readOnly={disabled}
        onChange={(e) => setInputValue(e.target.value)}
        onBlur={commitInput}
        className="text-[15px] font-bold font-mono text-text-primary min-w-[28px] w-[34px] text-center bg-transparent outline-none [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
      />
      <button
        onClick={() => onChange(1)}
        className={`w-6 h-6 rounded-full flex items-center justify-center transition-all ${
          canAdd ? 'bg-card border border-border-light text-text-muted active:bg-panel active:scale-90' : 'bg-transparent text-border-light cursor-not-allowed'
        }`}
        disabled={!canAdd}
      >
        <Plus className="w-3 h-3" />
      </button>
      <button
        onClick={() => adjustBy5(5)}
        className={`${step5Class} ${
          canAdd
            ? 'bg-card border border-border-light text-text-muted active:bg-panel active:scale-90'
            : 'bg-transparent text-border-light cursor-not-allowed'
        }`}
        disabled={!canAdd}
      >
        +5
      </button>
    </div>
  )
}
