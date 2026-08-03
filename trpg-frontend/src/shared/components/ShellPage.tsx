import type { ReactNode } from 'react'
import { ArrowLeft } from 'lucide-react'
import ShellBand, { type ShellBandVariant } from './ShellBand'

/** 外壳页面的统一骨架：纸板底 + 颗粒 + 满铺暗纹 + 顶部套印色带。
 *
 * 十一屏共用一个，所以「背景怎么铺」「色带多宽」「返回键长什么样」都只有一个
 * 答案，改一次全站生效。这也是上一轮换 `theme-coc` 时学到的：同一件事散在
 * 十一个页面里，改动必漏。
 *
 * `title` 传了就渲染内页顶栏（返回键 + 反白标题压在窄色带上）；不传就是落地屏
 * （登录 / 主页），色带是宽的、内容自己排。
 */
export default function ShellPage({
  title,
  onBack,
  children,
  contentClassName = '',
  band = 'landing',
}: {
  title?: string
  onBack?: () => void
  children: ReactNode
  contentClassName?: string
  /** 色带高度档位。内页（传了 `title`）恒为 slim；落地屏按反白内容多少选。 */
  band?: ShellBandVariant
}) {
  const slim = title != null
  return (
    <div className="theme-shell board-grain board-motif animate-screen-in relative min-h-full flex flex-col bg-page text-text-primary overflow-hidden">
      <ShellBand variant={slim ? 'slim' : band} />

      {slim && (
        <div className="relative z-10 flex items-center gap-2.5 px-4 pt-3.5 pb-3">
          {onBack && (
            <button
              onClick={onBack}
              className="press w-8 h-8 bg-card text-text-primary flex items-center justify-center flex-shrink-0"
            >
              <ArrowLeft className="w-[18px] h-[18px]" strokeWidth={2.5} />
            </button>
          )}
          {/* 反白压在色带上：色带是这一行的底衬，不是它上面的装饰 */}
          <h2 className="text-[17px] font-extrabold tracking-[0.06em] text-[#fff5ea]">{title}</h2>
        </div>
      )}

      {/* 🔴 内容**默认在剩余空间里居中**，空白平均分到上下两端。
          外壳这些页大多比屏幕短，顶对齐会把空白全堆在底部（创建房间那页为此
          改了三轮：贴底 → 贴内容 → 才发现问题不是按钮放哪，是空白没分匀）。
          放在这里而不是每页各自加，是因为**下一个新页面不会记得加**。
          内容长过屏幕时 min-h-full 让容器跟着内容长，居中自动失效，不会把
          顶部顶出滚动区。

          🔴 例外是**列表页**（我的游戏、选模组）：条目数量不定、会一直往下长，
          从上往下读才对，两条记录居中飘在屏幕中间很怪。它们传
          `contentClassName="justify-start"`。判据是「这一屏是一份**会增长的
          列表**，还是一组**固定的内容**」——选游戏那五张卡是后者（平台的菜单，
          不会变），所以照样居中。 */}
      <div className={`relative z-10 flex-1 flex flex-col justify-center py-4 ${contentClassName}`}>
        {children}
      </div>
    </div>
  )
}
