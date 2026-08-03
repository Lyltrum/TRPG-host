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
  const stepBase =
    'flex-none border flex items-center justify-center transition-all active:bg-brass-dark active:text-dossier'
  const step5Class = `${stepBase} w-[23px] h-[21px] typed text-[9.5px] font-semibold`
  const step1Class = `${stepBase} w-[23px] h-[21px]`
  const onCls = 'border-ink/50 bg-white/30 text-ink'
  const offCls = 'border-ink/20 text-ink-soft/50 cursor-not-allowed active:bg-transparent'

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
    // 技能行：本职方块 + 名字 + 基础值% + 五个方头键。表单里的一行，不是卡片。
    <div
      className={`flex items-center gap-1.5 py-1 border-b border-dotted border-ink/30 ${
        disabled ? 'opacity-70' : ''
      }`}
    >
      <div className="flex-1 min-w-0">
        <div className="text-[11.5px] text-ink flex items-center gap-1">
          <span className="truncate">{skill.name}</span>
          {slotted && (
            <span className="typed flex-none px-1 text-[9.5px] border border-brass-dark text-brass-dark">
              占槽
            </span>
          )}
        </div>
        {disabled && (
          <div className="text-[10.5px] text-ink-soft">{disabledReason ?? '建卡阶段不可加点'}</div>
        )}
      </div>
      <div className="text-[10.5px] text-ink-soft font-mono w-[26px] text-right">{base}%</div>
      <button
        onClick={() => adjustBy5(-5)}
        className={`${step5Class} ${canSub ? onCls : offCls}`}
        disabled={!canSub}
      >
        −5
      </button>
      <button
        onClick={() => onChange(-1)}
        className={`${step1Class} ${canSub ? onCls : offCls}`}
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
        className="text-[13.5px] font-bold font-mono text-ink min-w-[26px] w-[30px] text-center bg-transparent outline-none [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
      />
      <button
        onClick={() => onChange(1)}
        className={`${step1Class} ${canAdd ? onCls : offCls}`}
        disabled={!canAdd}
      >
        <Plus className="w-3 h-3" />
      </button>
      <button
        onClick={() => adjustBy5(5)}
        className={`${step5Class} ${canAdd ? onCls : offCls}`}
        disabled={!canAdd}
      >
        +5
      </button>
    </div>
  )
}
