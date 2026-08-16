import { useNavigate } from 'react-router-dom'
import { useState } from 'react'
import { DoorOpen, Hash } from 'lucide-react'
import ShellPage from '@/shared/components/ShellPage'
import { useAuthStore } from '@/stores/auth-store'
import { useRoomStore } from '@/stores/room-store'
import { getRoomInfo, joinRoomByCode } from '@/services/room'
import { friendlyErrorMessage } from '@/services/api-client'
import { useGameStore } from '@/stores/game-store'

export default function JoinRoomPage() {
  const navigate = useNavigate()
  const nickname = useAuthStore((s) => s.nickname)
  const setRoomIdentity = useRoomStore((s) => s.setRoomIdentity)
  const setModuleId = useRoomStore((s) => s.setModuleId)
  const setScene = useGameStore((s) => s.setScene)
  const [roomCode, setRoomCode] = useState('')
  const [error, setError] = useState('')
  const [joining, setJoining] = useState(false)

  const handleJoin = async () => {
    const code = roomCode.trim().toUpperCase()
    if (code.length < 4) {
      setError('请输入房间号')
      return
    }
    setError('')
    setJoining(true)
    try {
      const room = await joinRoomByCode(code, nickname || undefined)
      setRoomIdentity(room)
      // 访客拿不到选模组页的 sceneId：从房间预览补 moduleId（前情/开场依赖）
      try {
        const preview = await getRoomInfo(room.roomCode || code)
        const mid = preview.moduleId
        if (mid) {
          setModuleId(mid)
          setScene(mid, preview.moduleTitle ?? null)
        }
      } catch { /* 预览失败不挡进房 */ }
      // ★ 访客也要先进大厅——所有玩家到齐、都准备好之后才能一起进入建卡
      // 阶段，不能像以前那样直接跳过大厅、单独去建卡（见需求：全员到齐才能
      // 开始游戏）。
      navigate('/room/lobby')
    } catch (err) {
      setError(friendlyErrorMessage(err, '加入房间失败，请检查房间号'))
    } finally {
      setJoining(false)
    }
  }

  return (
    <ShellPage title="加入房间" onBack={() => navigate('/home')}>
      <div className="flex flex-col items-center px-5">
        <div className="press-soft w-14 h-14 mb-4 bg-card text-text-primary flex items-center justify-center">
          <DoorOpen className="w-7 h-7" strokeWidth={2} />
        </div>
        <p className="text-[13px] text-text-muted mb-6">输入房主分享的房间号</p>

        <div className="relative w-full max-w-[280px] mb-6">
          <Hash className="w-4 h-4 absolute left-3.5 top-1/2 -translate-y-1/2 text-text-dim" />
          <input
            value={roomCode}
            onChange={e => {
              const v = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 6)
              setRoomCode(v)
              setError('')
            }}
            placeholder="输入 6 位房间号"
            autoFocus
            maxLength={6}
            className="shell-field w-full pl-10 pr-4 py-3 text-[18px] font-mono font-bold tracking-[0.12em] placeholder:font-normal placeholder:tracking-normal placeholder:text-[14px]"
          />
        </div>

        {error && <p className="text-[12px] text-rust-dark mb-4">{error}</p>}

        <button
          onClick={handleJoin}
          disabled={roomCode.length < 4 || joining}
          className={`w-full max-w-[280px] py-3 text-[14px] font-extrabold tracking-[0.16em] indent-[0.16em] transition-all ${
            roomCode.length >= 4 && !joining
              ? 'press bg-rust text-[#fff5ea]'
              : 'border-2 border-dashed border-text-primary/35 text-text-muted cursor-not-allowed'
          }`}>
          {joining ? '加入中…' : '加入房间'}
        </button>
      </div>
    </ShellPage>
  )
}
