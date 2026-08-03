import type { ReactNode } from 'react'

/** 每步的统一版式：一句白话导语 + 分节内容 + 底部 blockers 清单
 * （重制设计 v2 §6 通用版式）。
 *
 * 🔴 步骤标题**不在这里**渲染：它跟「第 N / 8 步」是同一件东西，而只有
 * `CharacterWizardPage` 知道当前是第几步。标题由页面连着分栏号一起画成
 * 表单的栏头，这里只管这一步自己的内容。 */
export function StepShell({
  lead,
  children,
  blockers,
}: {
  lead: string
  children: ReactNode
  blockers?: string[]
}) {
  return (
    <div className="animate-screen-in">
      <p className="text-[11.5px] text-ink-soft leading-relaxed mb-3">{lead}</p>
      <div className="flex flex-col gap-3">{children}</div>
      {blockers && blockers.length > 0 && (
        <div className="mt-4 px-3 py-2.5 border border-brass-dark bg-white/25 text-[11.5px] text-brass-dark leading-relaxed space-y-1">
          {blockers.map((b, i) => (
            <div key={i}>{b}</div>
          ))}
        </div>
      )}
    </div>
  )
}

/** 一个分节。
 *
 * 🔴 **框线 + 压在线上的打字机标签**，不是圆角卡片——真实表单就是这么印的。
 * 标签底色用纸色把框线"咬断"，所以外层必须把 `--paper` 暴露出来
 * （`CharacterWizardPage` 的表单纸容器负责）。
 *
 * `accent` 是"必填 / 重点"那一档（信用评级、幸运、一键生成）：加粗黄铜边 +
 * 略亮的纸，一眼跟普通分节分得开。 */
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
    <div
      className={`relative px-2.5 pt-3.5 pb-2.5 ${
        accent ? 'border-[1.5px] border-brass-dark bg-white/20' : 'border border-ink/35'
      }`}
    >
      <span
        className={`typed absolute -top-[7px] left-2 px-1.5 text-[10.5px] ${
          accent ? 'text-brass-dark font-bold' : 'text-ink-soft'
        }`}
        style={{ backgroundColor: 'var(--paper)' }}
      >
        {title}
      </span>
      {tip && <p className="text-[10.5px] text-ink-soft leading-relaxed mb-2">{tip}</p>}
      {children}
    </div>
  )
}
