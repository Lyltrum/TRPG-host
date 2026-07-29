import { Minus, Plus } from 'lucide-react'
import type { ComponentType, CSSProperties } from 'react'

export interface AttributeAllocCardProps {
  attrKey: string
  label: string
  icon: ComponentType<{ className?: string; style?: CSSProperties }>
  color: string
  value: number
  inputValue: string
  min: number
  max: number
  /** 掷点池模式下步长只有 ±5（后端会校验 % 5 != 0），点数购买法额外给 ±1。 */
  step5Only: boolean
  presets: number[]
  presetDisabled: (preset: number) => boolean
  editable: boolean
  meaning?: string
  onAdjust: (delta: number) => void
  onSetValue: (value: number) => void
  onInputChange: (raw: string) => void
  onInputCommit: () => void
}

/** 单项属性卡：±/预设/输入/困难极难/一行白话说明（重制设计 v2 §6-步骤1）。 */
export function AttributeAllocCard({
  attrKey,
  label,
  icon: Icon,
  color,
  value,
  inputValue,
  min,
  max,
  step5Only,
  presets,
  presetDisabled,
  editable,
  meaning,
  onAdjust,
  onSetValue,
  onInputChange,
  onInputCommit,
}: AttributeAllocCardProps) {
  const btnClass =
    'w-7 h-7 rounded-full bg-card border border-border-light text-text-muted flex items-center justify-center active:bg-panel active:scale-90 transition-all disabled:opacity-30 disabled:active:scale-100'

  return (
    <div className="bg-input border border-border-light rounded-[6px] px-3.5 py-3">
      <div className="flex items-center gap-2.5 mb-1.5">
        <div
          className="w-7 h-7 rounded-lg flex items-center justify-center flex-shrink-0"
          style={{ backgroundColor: color + '18' }}
        >
          <Icon className="w-3.5 h-3.5" style={{ color }} />
        </div>
        <div className="text-[13px] font-semibold text-text-primary flex-1">
          {label} <span className="text-[10px] font-mono text-text-dim font-normal">{attrKey}</span>
        </div>
      </div>

      <div className="flex items-center justify-center gap-2.5 mb-1.5">
        {editable && (
          <button onClick={() => onAdjust(-5)} disabled={value - 5 < min} className={btnClass}>
            <Minus className="w-3 h-3" />
          </button>
        )}
        {editable && !step5Only && (
          <button onClick={() => onAdjust(-1)} disabled={value - 1 < min} className={btnClass}>
            <Minus className="w-3.5 h-3.5" />
          </button>
        )}
        <input
          type="number"
          inputMode="numeric"
          readOnly={!editable}
          value={inputValue}
          onChange={(e) => editable && onInputChange(e.target.value)}
          onBlur={onInputCommit}
          className="text-[22px] font-bold font-mono text-text-primary w-[58px] text-center bg-transparent outline-none [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
        />
        {editable && !step5Only && (
          <button onClick={() => onAdjust(1)} disabled={value + 1 > max} className={btnClass}>
            <Plus className="w-3.5 h-3.5" />
          </button>
        )}
        {editable && (
          <button onClick={() => onAdjust(5)} disabled={value + 5 > max} className={btnClass}>
            <Plus className="w-3 h-3" />
          </button>
        )}
      </div>

      <div className="text-center text-[10px] text-text-dim mb-2">
        困难 {Math.floor(value / 2)} · 极难 {Math.floor(value / 5)}
      </div>

      {editable && presets.length > 0 && (
        <div className="grid grid-cols-4 gap-1.5 mb-1.5">
          {presets.map((p) => (
            <button
              key={p}
              onClick={() => onSetValue(p)}
              disabled={presetDisabled(p)}
              className={`py-1 text-[11px] font-mono font-semibold rounded-[4px] border transition-all disabled:opacity-30 disabled:cursor-not-allowed ${
                value === p ? 'bg-brass text-white border-brass' : 'bg-card border-border-light text-text-muted active:bg-panel'
              }`}
            >
              {p}
            </button>
          ))}
        </div>
      )}

      {meaning && <div className="text-[10px] text-text-dim text-center">{meaning}</div>}
    </div>
  )
}
