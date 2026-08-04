import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Hash, Paperclip, Plus, ScrollText } from 'lucide-react'
import { useAuthStore } from '@/stores/auth-store'
import ShellPage from '@/shared/components/ShellPage'
import BrandMark from '@/shared/components/BrandMark'

// 登录后的首页——单独一个页面/路由，不再和登录/注册表单共用同一个
// LoginPage 组件（之前是同一个组件里用 isLoggedIn 切换两套内容）。
export default function HomePage() {
  const navigate = useNavigate()
  const isLoggedIn = useAuthStore((s) => s.isLoggedIn)
  const nickname = useAuthStore((s) => s.nickname)

  useEffect(() => {
    if (!isLoggedIn) navigate('/auth/login', { replace: true })
  }, [isLoggedIn, navigate])

  if (!isLoggedIn) return null

  return (
    <ShellPage>
      <button
        onClick={() => navigate('/home/profile')}
        title="修改个人信息"
        className="press-soft absolute bottom-4 left-4 z-20 flex items-center gap-1.5 bg-card pl-1 pr-2.5 py-1"
      >
        <div className="w-[22px] h-[22px] bg-text-primary text-page text-[11px] font-extrabold flex items-center justify-center flex-shrink-0">
          {nickname?.charAt(0) || '?'}
        </div>
        <span className="text-[11px] font-semibold text-text-primary max-w-[80px] truncate">{nickname}</span>
      </button>

      {/* 品牌区整块落在色带里（`band="landing"` 的高度就是按它定的） */}
      <div className="flex flex-col items-center pt-10 px-5 pb-6 text-center">
        <div className="press-soft w-[62px] h-[62px] mb-3 bg-card text-text-primary flex items-center justify-center">
          <BrandMark className="w-[60%] h-[60%]" />
        </div>
        <h1 className="text-[22px] font-extrabold text-[#fff5ea] tracking-[0.1em] px-2">
          AI桌游主持人
        </h1>
        <p className="text-[11.5px] text-[#fff5ea]/85 tracking-[0.06em] mt-1">
          AI 智能主持 · 多游戏聚会平台
        </p>
      </div>

      {/* 🔴 入口占据**剩余空间的中间**，不是贴着品牌区往下堆。
          这一屏按钮不多，靠自然流排会把内容全挤在上半屏、下面空一大片
          （真机反馈）。`flex-1 + justify-center` 让空白平均分到上下两侧。 */}
      <div className="flex-1 flex flex-col justify-center px-5 gap-3 py-6">
        <button
          className="press w-full py-3 flex items-center justify-center gap-2 text-[14px] font-extrabold tracking-[0.16em] indent-[0.16em] bg-rust text-[#fff5ea]"
          onClick={() => navigate('/home/join')}
        >
          <Hash className="w-[18px] h-[18px]" />
          加入房间
        </button>
        <button
          className="press w-full py-3 flex items-center justify-center gap-2 text-[14px] font-extrabold tracking-[0.16em] indent-[0.16em] bg-ink-blue text-[#fff5ea]"
          onClick={() => navigate('/home/create')}
        >
          <Plus className="w-[18px] h-[18px]" />
          创建房间
        </button>
        <button
          className="press w-full py-3 flex items-center justify-center gap-2 text-[14px] font-extrabold tracking-[0.16em] indent-[0.16em] bg-card text-text-primary"
          onClick={() => navigate('/home/my-rooms')}
        >
          <ScrollText className="w-[18px] h-[18px]" />
          我的游戏
        </button>
        {/* 🔴 导入模组是**备料**，跟开局在时间上是分开的（可能是聚会前几天），
            而且转换要跑 5–26 分钟——它不可能是建房流程里的一步。所以入口在顶层，
            不只挂在选模组页底下（`exec/29 §7.2`）。 */}
        <button
          className="press w-full py-3 flex items-center justify-center gap-2 text-[14px] font-extrabold tracking-[0.16em] indent-[0.16em] bg-card text-text-primary"
          onClick={() => navigate('/home/modules')}
        >
          <Paperclip className="w-[18px] h-[18px]" />
          我的模组
        </button>
      </div>

      <p className="text-center pb-5 text-text-dim text-[11px]">
        AI桌游主持人 © 2026
      </p>
    </ShellPage>
  )
}
