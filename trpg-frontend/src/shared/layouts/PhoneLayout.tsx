import type { ReactNode } from 'react'
import { useEffect, useRef } from 'react'
import { useLocation } from 'react-router-dom'

interface PhoneLayoutProps {
  children: ReactNode
}

export default function PhoneLayout({ children }: PhoneLayoutProps) {
  const mainRef = useRef<HTMLElement>(null)
  const { pathname } = useLocation()

  // <main> 是横跨所有路由、从不重新挂载的滚动容器——SPA 导航不像整页跳转
  // 那样会自动把滚动位置归零。上一页往下滑过之后再切页，新页面会直接
  // 顶着那个滚动偏移渲染，看起来像是内容"消失"了、面板位置不对。
  useEffect(() => {
    mainRef.current?.scrollTo(0, 0)
  }, [pathname])

  // overscroll-contain：这层照常滚，但滚到底/滚到顶时**不把滚动传给页面**
  // （iOS Safari 真机：不加这条，滑到底再往上拽会把整个页面带起来）。
  // 用 contain 而不是 none —— none 会连它自己的橡皮筋一起关掉，那是这层唯一
  // 该保留的手感。页面级的 none 在 styles.css 的 html/body 上。
  return (
      <main ref={mainRef} className="animate-screen-in h-full overflow-y-auto overflow-x-hidden overscroll-contain">{children}</main>
  )
}
