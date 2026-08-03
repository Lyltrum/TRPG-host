import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { ScrollText } from 'lucide-react'
import ShellPage from '@/shared/components/ShellPage'
import { getRoomInfo, type RoomPreview } from '@/services/room'
import { friendlyErrorMessage } from '@/services/api-client'

// 复盘摘要目前后端还没有真的生成——这里先展示一段占位文案，模拟设计稿里
// 「复盘摘要异步生成，先 pending 再补上」的体验（见 API 接口对齐规范 §复盘）。
const PLACEHOLDER_RECAP =
  '本局的调查过程将被整理成复盘摘要——目前后端的摘要生成还在开发中，这段文字只是占位效果。'

export default function ReviewPage() {
  const navigate = useNavigate()
  const { roomCode } = useParams<{ roomCode: string }>()
  const [room, setRoom] = useState<RoomPreview | null>(null)
  const [error, setError] = useState('')
  const [generating, setGenerating] = useState(true)

  useEffect(() => {
    if (!roomCode) return
    getRoomInfo(roomCode)
      .then(setRoom)
      .catch((err) => setError(friendlyErrorMessage(err, '加载复盘失败')))
  }, [roomCode])

  useEffect(() => {
    const timer = setTimeout(() => setGenerating(false), 900)
    return () => clearTimeout(timer)
  }, [])

  return (
    <ShellPage title="复盘" onBack={() => navigate('/home/my-rooms')}>
      <div className="px-5 flex flex-col gap-3.5">
        {error && <p className="text-[11.5px] text-rust-dark text-center">{error}</p>}

        {room && (
          <>
            <div className="press-soft bg-card p-3.5">
              <div className="text-[15px] font-extrabold text-text-primary">{room.roomName}</div>
              <div className="text-[11.5px] text-text-muted mt-0.5">{room.moduleTitle || '未知模组'} · 已完成</div>
            </div>

            <div className="press-soft bg-card p-3.5">
              <span className="inline-flex items-center gap-1.5 text-[10.5px] font-bold tracking-[0.14em] bg-text-primary text-page px-2 py-[3px] mb-2.5">
                <ScrollText className="w-[13px] h-[13px]" /> 案件回顾
              </span>
              {generating ? (
                <p className="text-[13px] text-text-dim py-4 text-center animate-pulse">复盘摘要生成中…</p>
              ) : (
                <p className="text-[13px] text-text-body leading-[1.85]">{PLACEHOLDER_RECAP}</p>
              )}
            </div>

            <div className="press-soft bg-card p-3.5">
              <span className="inline-block text-[10.5px] font-bold tracking-[0.14em] bg-text-primary text-page px-2 py-[3px] mb-2.5">
                参与调查员
              </span>
              <div className="flex flex-col gap-1.5">
                {room.players.map((p) => (
                  <div key={p.playerId} className="flex items-center gap-2.5 px-2.5 py-1.5 border-2 border-text-primary/25">
                    <div className="w-8 h-8 border-2 border-text-primary bg-page flex items-center justify-center text-[14px] flex-shrink-0">🔍</div>
                    <div className="flex-1 min-w-0">
                      <div className="text-[13px] font-bold text-text-primary">{p.nickname}</div>
                      <div className="text-[10.5px] text-text-muted">{p.isHost ? '房主' : '玩家'}</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </>
        )}
      </div>
    </ShellPage>
  )
}
