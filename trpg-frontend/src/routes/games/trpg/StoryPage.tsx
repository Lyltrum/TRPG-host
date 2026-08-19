import { useNavigate } from 'react-router-dom'
import { useEffect, useMemo, useState } from 'react'
import { ArrowLeft, BookOpen } from 'lucide-react'
import { useGameStore } from '@/stores/game-store'
import { useRoomStore } from '@/stores/room-store'
import { getScenarioById } from '@/config/games'
import { disconnectWebSocket, friendlyErrorMessage, getAuthToken, sdk } from '@/services/api-client'
import type { ModuleDetail } from 'trpg-sdk'

export default function StoryPage() {
  const navigate = useNavigate()
  const sceneId = useGameStore((s) => s.sceneId)
  const roomModuleId = useRoomStore((s) => s.moduleId)
  // 房主选模组时 sceneId 在 game-store；访客/刷新后优先 room-store.moduleId
  const moduleId = sceneId || roomModuleId

  const localScenario = useMemo(
    () => (moduleId ? getScenarioById(moduleId) : undefined),
    [moduleId],
  )

  const [detail, setDetail] = useState<ModuleDetail | null>(null)
  const [loading, setLoading] = useState(Boolean(moduleId))
  const [loadError, setLoadError] = useState('')
  const [confirmExit, setConfirmExit] = useState(false)

  useEffect(() => {
    if (!moduleId) {
      setLoading(false)
      setDetail(null)
      return
    }
    const token = getAuthToken()
    if (!token) {
      setDetail(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setLoadError('')
    sdk.modules
      .getDetail(moduleId, token)
      .then((mod) => {
        if (!cancelled) setDetail(mod)
      })
      .catch((err) => {
        if (!cancelled) {
          setDetail(null)
          setLoadError(friendlyErrorMessage(err, '加载模组前情失败'))
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [moduleId])

  const handleExit = () => {
    disconnectWebSocket()
    navigate('/home')
  }

  // 退出确认 = 压在桌上的一张便条（同等待大厅），不是深色对话框
  const exitConfirm = confirmExit && (
    <div className="fixed inset-0 z-40 bg-black/60 flex items-center justify-center px-8" onClick={() => setConfirmExit(false)}>
      <div
        className="theme-paper leaf paper-grain bg-book text-ink p-4 pl-5 w-full max-w-[300px] border-l-[3px] border-l-rust"
        onClick={(e) => e.stopPropagation()}
      >
        <p className="text-[12.5px] text-ink text-center leading-relaxed mb-3.5">
          确定要退出游戏吗？房间会保留，之后可以从「我的游戏」继续。
        </p>
        <div className="flex gap-2">
          <button onClick={() => setConfirmExit(false)}
            className="cut-corner flex-1 py-2 border border-ink/35 bg-white/25 text-ink-soft text-[12px] font-semibold active:scale-[0.97]">
            取消
          </button>
          <button onClick={handleExit}
            className="cut-corner flex-1 py-2 bg-rust-dark text-book text-[12px] font-semibold active:scale-[0.97]">
            确认退出
          </button>
        </div>
      </div>
    </div>
  )

  const exitButton = (
    <button
      onClick={() => setConfirmExit(true)}
      className="cut-corner absolute top-4 left-4 w-[34px] h-[34px] bg-input border border-border-mid flex items-center justify-center text-text-body z-10 active:bg-panel active:scale-[0.94] transition-all"
    >
      <ArrowLeft className="w-[18px] h-[18px]" strokeWidth={2.5} />
    </button>
  )

  const title = detail?.title || localScenario?.name
  const titleEn = localScenario?.nameEn
  const storyLabel = localScenario?.storyLabel || (title ? `可玩 · ${title}` : '')
  const storyPages =
    (detail?.storyPages && detail.storyPages.length > 0
      ? detail.storyPages
      : detail?.playerIntro
        ? [detail.playerIntro]
        : detail?.synopsis
          ? [detail.synopsis]
          : localScenario?.storyPages?.length
            ? localScenario.storyPages
            : []) as string[]

  if (!moduleId) {
    return (
      // 🔴 `bg-card` = 桌面木色。木纹是 multiply 混合，底下没颜色等于没铺。
      <div className="theme-coc desk-grain desk-lamp desk-sigil bg-card min-h-full flex flex-col justify-center px-7 py-10 relative">
        {exitConfirm}
        {exitButton}
        <div className="relative z-10 text-center text-text-muted">
          <BookOpen className="w-12 h-12 mx-auto mb-4 text-brass-dark" />
          <p className="text-[13px] text-text-body">未选择模组</p>
          <button
            onClick={() => navigate('/home/create/games')}
            className="cut-corner mt-6 px-5 py-2.5 bg-brass-dark border border-brass text-text-primary text-[12.5px] font-semibold active:translate-y-[1px]"
          >
            返回选择游戏
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="theme-coc desk-grain desk-lamp desk-sigil bg-card min-h-full flex flex-col pt-14 pb-4 relative">
      {exitConfirm}
      {exitButton}

      {/* 🔴 这一屏跟前后两屏**不同族**：它是唯一一屏真的要"读"的东西。
          所以做成一卷摊在桌上的卷轴——两根木轴夹住纸，纸的上下缘卷进轴里，
          进屏时展开。守秘人叙事当初做成整页书被判"很怪"，是因为他是个在场
          的人、说话不该长得像出版物；模组前情本来就是一段读物。

          轴比纸宽（轴头要露在纸外面），所以两者的横向留白不一样。 */}
      <div className="relative z-10 flex-1 min-h-0 flex flex-col mx-[30px]">
        <div className="scroll-rod -mx-2" />

        <div className="theme-paper paper-grain animate-unroll relative flex-1 min-h-0 flex flex-col bg-book text-ink shadow-[0_10px_26px_rgba(0,0,0,0.34)]">
          <div className="scroll-curl scroll-curl-top" />
          <div className="scroll-curl scroll-curl-bottom" />

          {/* 正文长了在纸内部滚，两根轴不动 */}
          <div className="relative z-[1] flex-1 min-h-0 overflow-y-auto flex flex-col px-6 pt-8 pb-7">
            <div className="typed text-[10.5px] text-ink-soft mb-4">{storyLabel}</div>
            <h1 className="text-[27px] font-bold text-ink leading-[1.25] mb-1.5 text-balance">
              {title || '…'}
            </h1>
            {titleEn && (
              <p className="font-mono text-[11.5px] text-ink-soft tracking-[0.05em]">{titleEn}</p>
            )}

            {/* 卷首饰线：一道细线 + 一枚菱形。书才有的东西，登记表不会有 */}
            <div className="flex items-center gap-2 my-6">
              <span className="h-px w-10 bg-ink/45" />
              <span className="w-[5px] h-[5px] rotate-45 bg-ink/45" />
              <span className="h-px flex-1 bg-ink/20" />
            </div>

            <div className="text-[14px] leading-[1.95] text-ink">
              {loading && <p className="text-ink-soft">正在加载前情…</p>}
              {!loading && loadError && <p className="text-rust-dark">{loadError}</p>}
              {!loading && !loadError && storyPages.length === 0 && (
                <p className="text-ink-soft">暂无玩家可见的开场介绍（structured 未就绪）。</p>
              )}
              {/* 首行缩进两字：中文书籍的排版惯例。这里是真读物，所以成立 */}
              {!loading &&
                storyPages.map((page, idx) => (
                  <p
                    key={idx}
                    className={`indent-[2em] ${idx < storyPages.length - 1 ? 'mb-3' : ''}`}
                  >
                    {page}
                  </p>
                ))}
            </div>

            <div className="flex-1 min-h-[18px]" />

            <button
              onClick={() => navigate('/room/character')}
              disabled={loading}
              className={`mt-7 self-start px-7 py-3 text-[13.5px] font-bold tracking-[0.18em] indent-[0.18em] transition-all ${
                loading
                  ? 'border border-ink/25 text-ink-soft cursor-not-allowed'
                  : 'cut-corner bg-brass-dark text-book active:translate-y-[1px]'
              }`}
            >
              继续 →
            </button>
          </div>
        </div>

        <div className="scroll-rod -mx-2" />
      </div>
    </div>
  )
}
