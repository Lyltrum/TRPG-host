import type { ReactNode } from 'react'

type BadgeVariant = 'success' | 'info' | 'default' | 'warning'

interface BadgeProps {
  variant?: BadgeVariant
  children: ReactNode
}

/** 🔴 实色描边，不是浅色透明底：12% 的底压在纸板上几乎看不出来
 *  （这一轮已经因为同一条改过难度徽标和游戏索引色，这里是漏网的第三处）。
 *  同理方角不是圆角胶囊，字号也从 10px 抬到 10.5px 的中文小字下限。 */
const variantStyles: Record<BadgeVariant, string> = {
  success: 'border-mold text-mold',
  info: 'border-ink-blue text-ink-blue',
  default: 'border-text-muted text-text-muted',
  warning: 'border-brass-bright text-brass-dark',
}

export default function Badge({ variant = 'default', children }: BadgeProps) {
  return (
    <span
      className={`inline-block px-2 py-[1px] border-2 text-[10.5px] font-bold ${variantStyles[variant]}`}
    >
      {children}
    </span>
  )
}
