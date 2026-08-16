import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { DoorOpen } from 'lucide-react'
import ShellPage from '@/shared/components/ShellPage'
import { useAuthStore } from '@/stores/auth-store'
import { useRoomStore } from '@/stores/room-store'
import { useGameStore } from '@/stores/game-store'
import { getRoomInfo, joinRoomByCode } from '@/services/room'
import { registerAsGuest } from '@/services/auth'
import { friendlyErrorMessage } from '@/services/api-client'

/**
 * 受邀落地页 `/join/:roomCode`——朋友点开分享链接看到的第一屏。
 *
 * 🔴 这一屏存在的理由是**把入房摩擦压到"输个名字"**：原来的路径是
 * 注册页（账号+密码+昵称）→ 登录 → 首页 → 加入房间 → 手输房间码，
 * 聚会时每个人走一遍。
 *
 * 已登录的人直接进（他已经有身份了，不该再问一次名字）。
 */
export default function InviteLandingPage() {
  const navigate = useNavigate()
  const { roomCode = '' } = useParams()
  const code = roomCode.toUpperCase()

  const isLoggedIn = useAuthStore((s) => s.isLoggedIn)
  const savedNickname = useAuthStore((s) => s.nickname)
  const loginToStore = useAuthStore((s) => s.login)
  const setRoomIdentity = useRoomStore((s) => s.setRoomIdentity)
  const setModuleId = useRoomStore((s) => s.setModuleId)
  const setScene = useGameStore((s) => s.setScene)

  const [preview, setPreview] = useState<{ roomName?: string; moduleTitle?: string | null } | null>(null)
  const [nickname, setNickname] = useState(savedNickname ?? '')
  const [error, setError] = useState('')
  const [joining, setJoining] = useState(false)

  // 先给他看清楚要进的是哪个房间——链接是别人发来的，进之前该知道是什么。
  useEffect(() => {
    let alive = true
    getRoomInfo(code)
      .then((info) => {
        if (alive) setPreview({ roomName: info.roomName, moduleTitle: info.moduleTitle ?? null })
      })
      .catch(() => {
        if (alive) setError('找不到这个房间，问问房主链接对不对')
      })
    return () => {
      alive = false
    }
  }, [code])

  const handleJoin = async () => {
    const name = nickname.trim()
    if (!isLoggedIn && !name) {
      setError('先起个名字')
      return
    }
    setError('')
    setJoining(true)
    try {
      if (!isLoggedIn) {
        const res = await registerAsGuest(name)
        loginToStore(res.token, res.userId, name)
      }
      const room = await joinRoomByCode(code, name || undefined)
      setRoomIdentity(room)
      // 访客拿不到选模组页的 sceneId：从房间预览补 moduleId（前情/开场依赖）
      try {
        const info = await getRoomInfo(room.roomCode || code)
        if (info.moduleId) {
          setModuleId(info.moduleId)
          setScene(info.moduleId, info.moduleTitle ?? null)
        }
      } catch { /* 预览失败不挡进房 */ }
      navigate('/room/lobby')
    } catch (err) {
      setError(friendlyErrorMessage(err, '加入房间失败'))
    } finally {
      setJoining(false)
    }
  }

  return (
    <ShellPage title="受邀加入" onBack={() => navigate('/home')}>
      <div className="flex flex-col items-center px-5">
        <div className="press-soft w-14 h-14 mb-4 bg-card text-text-primary flex items-center justify-center">
          <DoorOpen className="w-7 h-7" strokeWidth={2} />
        </div>

        <p className="text-[13px] text-text-muted mb-1">有人邀请你加入</p>
        <p className="text-[17px] font-semibold text-text-primary mb-1">{preview?.roomName || '房间'}</p>
        {preview?.moduleTitle && <p className="text-[12px] text-text-muted mb-1">模组 · {preview.moduleTitle}</p>}
        <p className="font-mono text-[13px] text-text-dim tracking-[0.18em] mb-6">{code}</p>

        {!isLoggedIn && (
          <input
            value={nickname}
            onChange={(e) => {
              setNickname(e.target.value.slice(0, 12))
              setError('')
            }}
            placeholder="你叫什么？"
            autoFocus
            maxLength={12}
            className="shell-field w-full max-w-[280px] px-4 py-3 text-[15px] mb-2 text-center"
          />
        )}

        {error && <p className="text-[12px] text-rust mb-2 text-center">{error}</p>}

        <button
          type="button"
          onClick={handleJoin}
          disabled={joining || !preview}
          className="press-hard w-full max-w-[280px] mt-2 py-3 bg-accent text-book text-[15px] font-semibold disabled:opacity-50 active:scale-[0.98]"
        >
          {joining ? '正在进入…' : isLoggedIn ? '进入房间' : '进入房间'}
        </button>

        {!isLoggedIn && (
          <p className="text-[11px] text-text-dim mt-4 text-center leading-relaxed">
            会在这台设备上给你留一个身份，
            <br />
            换手机或清了缓存就得重新进一次
          </p>
        )}
      </div>
    </ShellPage>
  )
}
