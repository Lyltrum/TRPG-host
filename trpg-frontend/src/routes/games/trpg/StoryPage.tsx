import { useNavigate } from 'react-router-dom'
import { useEffect, useMemo, useState } from 'react'
import { ArrowLeft, BookOpen } from 'lucide-react'
import { useGameStore } from '@/stores/game-store'
import { useRoomStore } from '@/stores/room-store'
import { getScenarioById } from '@/config/games'
import { disconnectWebSocket, friendlyErrorMessage, sdk } from '@/services/api-client'
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
    let cancelled = false
    setLoading(true)
    setLoadError('')
    sdk.modules
      .getDetail(moduleId)
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

  const exitConfirm = confirmExit && (
    <div className="fixed inset-0 z-40 bg-black/60 flex items-center justify-center px-8" onClick={() => setConfirmExit(false)}>
      <div className="bg-[#1a1620] border border-[rgba(255,255,255,0.12)] rounded-md p-5 w-full max-w-[300px]" onClick={(e) => e.stopPropagation()}>
        <p className="text-sm text-[#d4cfc8] text-center mb-4">确定要退出游戏吗？房间会保留，之后可以从「我的游戏」继续。</p>
        <div className="flex gap-2">
          <button onClick={() => setConfirmExit(false)}
            className="flex-1 py-2 rounded-sm bg-[rgba(255,255,255,0.08)] border border-[rgba(255,255,255,0.12)] text-[#a09888] text-xs font-medium">
            取消
          </button>
          <button onClick={handleExit}
            className="flex-1 py-2 rounded-sm bg-[#c04040] text-white text-xs font-medium active:bg-[#a03030]">
            确认退出
          </button>
        </div>
      </div>
    </div>
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
      <div className="min-h-screen bg-gradient-to-b from-[#1a1620] to-[#0d0b10] flex flex-col justify-center px-7 py-10 relative">
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_50%_30%,rgba(112,80,160,0.08),transparent_70%)] pointer-events-none" />
        {exitConfirm}
        <button
          onClick={() => setConfirmExit(true)}
          className="absolute top-4 left-4 w-[34px] h-[34px] rounded-full bg-[rgba(255,255,255,0.08)] border border-[rgba(255,255,255,0.12)] flex items-center justify-center text-[#a09888] z-10"
        >
          <ArrowLeft className="w-[18px] h-[18px]" />
        </button>
        <div className="text-center text-[#9088a0]">
          <BookOpen className="w-12 h-12 mx-auto mb-4 opacity-50" />
          <p className="text-sm">未选择模组</p>
          <button
            onClick={() => navigate('/home/create/games')}
            className="mt-6 px-5 py-2.5 rounded-sm bg-brass text-white text-xs font-semibold"
          >
            返回选择游戏
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-gradient-to-b from-[#1a1620] to-[#0d0b10] flex flex-col justify-center px-7 py-10 relative">
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_50%_30%,rgba(112,80,160,0.08),transparent_70%)] pointer-events-none" />
      {exitConfirm}
      <button
        onClick={() => setConfirmExit(true)}
        className="absolute top-4 left-4 w-[34px] h-[34px] rounded-full bg-[rgba(255,255,255,0.08)] border border-[rgba(255,255,255,0.12)] flex items-center justify-center text-[#a09888] z-10"
      >
        <ArrowLeft className="w-[18px] h-[18px]" />
      </button>

      <div className="font-mono text-[11px] tracking-[0.15em] text-[#706090] mb-5">
        {storyLabel}
      </div>
      <h1 className="text-[28px] font-bold text-[#eeead8] leading-[1.25] mb-2">
        {title || '…'}
      </h1>
      {titleEn ? (
        <p className="font-mono text-xs text-[#9088a0] mb-8 tracking-[0.05em]">
          {titleEn}
        </p>
      ) : (
        <div className="mb-8" />
      )}
      <div className="w-10 h-px bg-[#504860] mb-7" />
      <div className="text-sm leading-[1.9] text-[#c8c0b8]">
        {loading && <p className="text-[#9088a0]">正在加载前情…</p>}
        {!loading && loadError && (
          <p className="text-[#c08080]">{loadError}</p>
        )}
        {!loading && !loadError && storyPages.length === 0 && (
          <p className="text-[#9088a0]">暂无玩家可见的开场介绍（structured 未就绪）。</p>
        )}
        {!loading &&
          storyPages.map((page, idx) => (
            <p
              key={idx}
              className={idx < storyPages.length - 1 ? 'mb-4' : ''}
            >
              {page}
            </p>
          ))}
      </div>
      <button
        onClick={() => navigate('/room/character')}
        disabled={loading}
        className="mt-10 self-start px-6 py-3.5 rounded-sm bg-brass text-white text-sm font-semibold active:bg-brass-dark transition-all disabled:opacity-50"
      >
        继续 →
      </button>
    </div>
  )
}
