import type { ReactNode } from 'react'

/** 每步的统一版式：标题 + 一句白话导语 + 分节内容 + 底部 blockers 清单
 * （重制设计 v2 §6 通用版式）。 */
export function StepShell({
  title,
  lead,
  children,
  blockers,
}: {
  title: string
  lead: string
  children: ReactNode
  blockers?: string[]
}) {
  return (
    <div className="px-5 pb-24 animate-screen-in">
      <div className="mb-4">
        <h2 className="text-lg font-bold text-text-primary mb-1">{title}</h2>
        <p className="text-[12px] text-text-muted">{lead}</p>
      </div>
      <div className="space-y-3">{children}</div>
      {blockers && blockers.length > 0 && (
        <div className="mt-4 px-3.5 py-2.5 bg-[#fdf3e0] border border-[#e0c088] rounded-[6px] text-[12px] text-[#8a6a2a] space-y-1">
          {blockers.map((b, i) => (
            <div key={i}>{b}</div>
          ))}
        </div>
      )}
    </div>
  )
}

/** 一个分节：h3 小标题 + 可选 tip 单行灰字提示 + 内容。 */
export function StepSection({
  title,
  tip,
  children,
  accent,
}: {
  title: string
  tip?: string
  children: ReactNode
  accent?: boolean
}) {
  return (
    <div className={`bg-card border rounded-md p-[18px] ${accent ? 'border-brass bg-[#fdfaf4]' : 'border-border-light'}`}>
      <h4 className="text-[12px] font-semibold text-brass-dark uppercase tracking-[0.08em] mb-1.5">{title}</h4>
      {tip && <p className="text-[11px] text-text-dim mb-2.5">{tip}</p>}
      {children}
    </div>
  )
}
