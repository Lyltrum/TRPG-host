import { Minus, Plus } from 'lucide-react'

export interface AttributeAllocCardProps {
  attrKey: string
  label: string
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
  /** 难度分档，来自后端 ruleset（`successTiers`）。🔴 除数不在前端写死：
   * 服务端判定检定成功等级用的是同一份，抄第二份必然在改规则时漏掉一处。 */
  tiers: { id: string; label: string; divisor: number }[]
  onAdjust: (delta: number) => void
  onSetValue: (value: number) => void
  onInputChange: (raw: string) => void
  onInputCommit: () => void
}

/** 单项属性：**一行**（重制设计 v2 §6-步骤1 的卡片压成行）。
 *
 * 🔴 原来一项一张卡（图标 + 名称 + 五个按钮 + 一排预设 + 说明），八张竖排在
 * 手机上要滑很久——而属性分配恰恰需要**互相对照着调**。压成行之后八项能同屏
 * 看见，五个按钮一个没减，说明与预设各自降级成行下的小字与小方块。 */
export function AttributeAllocCard({
  attrKey,
  label,
  value,
  inputValue,
  min,
  max,
  step5Only,
  presets,
  presetDisabled,
  editable,
  meaning,
  tiers,
  onAdjust,
  onSetValue,
  onInputChange,
  onInputCommit,
}: AttributeAllocCardProps) {
  const btnClass =
    'w-[23px] h-[21px] flex-none border border-ink/50 bg-white/30 text-ink flex items-center justify-center active:bg-brass-dark active:text-dossier transition-all disabled:border-ink/20 disabled:text-ink-faint disabled:bg-transparent'

  return (
    <div className="border-b border-dotted border-ink/30 pb-1 mb-1 last:border-b-0">
      <div className="flex items-center gap-1.5 py-1">
        <div className="flex-1 min-w-0 text-[11.5px] text-ink truncate">
          {label} <span className="typed text-[10.5px] text-ink-soft">{attrKey}</span>
        </div>
        {editable && (
          <button onClick={() => onAdjust(-5)} disabled={value - 5 < min} className={`${btnClass} typed text-[9.5px]`}>
            −5
          </button>
        )}
        {editable && !step5Only && (
          <button onClick={() => onAdjust(-1)} disabled={value - 1 < min} className={btnClass}>
            <Minus className="w-3 h-3" />
          </button>
        )}
        <input
          type="number"
          inputMode="numeric"
          readOnly={!editable}
          value={inputValue}
          onChange={(e) => editable && onInputChange(e.target.value)}
          onBlur={onInputCommit}
          className="text-[15px] font-bold font-mono text-ink w-[34px] text-center bg-transparent outline-none [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
        />
        {editable && !step5Only && (
          <button onClick={() => onAdjust(1)} disabled={value + 1 > max} className={btnClass}>
            <Plus className="w-3 h-3" />
          </button>
        )}
        {editable && (
          <button onClick={() => onAdjust(5)} disabled={value + 5 > max} className={`${btnClass} typed text-[9.5px]`}>
            +5
          </button>
        )}
      </div>

      {/* 🔴 预设值、含义、半值/五分之一**共用一行**。
          它们全是"参考信息"，各占一行会让一项属性长到三行、八项属性滑不完；
          半值那组此前挤在上一行右侧只有 42px，真机上被折成了两行。 */}
      <div className="flex items-center gap-2 pb-0.5">
        {editable && presets.length > 0 && (
          <div className="flex gap-1 flex-none">
            {presets.map((p) => (
              <button
                key={p}
                onClick={() => onSetValue(p)}
                disabled={presetDisabled(p)}
                className={`w-[30px] py-[1px] typed text-[10.5px] font-semibold border transition-all disabled:border-ink/20 disabled:text-ink-faint disabled:bg-transparent ${
                  value === p
                    ? 'bg-brass-dark text-dossier border-brass-dark'
                    : 'border-ink/30 bg-white/20 text-ink-soft'
                }`}
              >
                {p}
              </button>
            ))}
          </div>
        )}
        {meaning && (
          <div className="flex-1 min-w-0 truncate text-[10.5px] text-ink-soft">{meaning}</div>
        )}
        {tiers.length > 0 && (
          <span className="font-mono text-[10.5px] text-ink-soft whitespace-nowrap flex-none">
            {tiers.map((t) => Math.floor(value / t.divisor)).join(' / ')}
          </span>
        )}
      </div>
    </div>
  )
}
