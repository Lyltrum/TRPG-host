import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { LogOut } from 'lucide-react'
import ShellPage from '@/shared/components/ShellPage'
import { useAuthStore } from '@/stores/auth-store'
import { useRoomStore } from '@/stores/room-store'
import { useCharacterStore } from '@/stores/character-store'
import { updateProfile, fetchMe, logout as logoutFromServer } from '@/services/auth'
import { friendlyErrorMessage } from '@/services/api-client'

export default function ProfilePage() {
  const navigate = useNavigate()
  const [account, setAccount] = useState('')
  const nickname = useAuthStore((s) => s.nickname)
  const setNickname = useAuthStore((s) => s.setNickname)
  const clearAuthStore = useAuthStore((s) => s.logout)
  const resetRoomStore = useRoomStore((s) => s.reset)
  const clearCharacterStore = useCharacterStore((s) => s.clear)
  const [draft, setDraft] = useState(nickname || '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    fetchMe().then((me) => { if (me) setAccount(me.account) })
  }, [])

  const handleSave = async () => {
    if (!draft.trim() || draft === nickname) return
    setSaving(true)
    setError('')
    setSaved(false)
    try {
      const res = await updateProfile(draft.trim())
      setNickname(res.nickname)
      setSaved(true)
    } catch (err) {
      setError(friendlyErrorMessage(err, '保存失败'))
    } finally {
      setSaving(false)
    }
  }

  const handleLogout = async () => {
    // 之前只清了 auth-store 的内存状态，没调用 services/auth.ts 里真正撤销
    // 后端会话 + 清掉 localStorage token 的 logout()——退出后刷新页面，
    // App 的 fetchMe() 会拿着还没失效的 token 把用户自动重新登进去。
    await logoutFromServer().catch(() => {
      // 后端调用失败也无所谓，本地状态照样清掉，让用户回到登录页。
    })
    clearAuthStore()
    resetRoomStore()
    clearCharacterStore()
    navigate('/auth/login')
  }

  return (
    <ShellPage title="个人信息" onBack={() => navigate('/home')}>
      <div className="px-5 flex flex-col items-center pt-5 pb-7">
        <div className="press-soft w-16 h-16 bg-text-primary text-page text-2xl font-extrabold flex items-center justify-center">
          {(draft || nickname)?.charAt(0) || '?'}
        </div>
      </div>

      <div className="px-5 flex flex-col gap-3">
        <div className="press-soft bg-card p-3.5">
          <label className="text-[10.5px] font-bold text-text-muted mb-1.5 block tracking-[0.1em]">昵称</label>
          <input
            value={draft}
            onChange={(e) => { setDraft(e.target.value); setSaved(false) }}
            placeholder="输入昵称"
            className="shell-field w-full px-3 py-2 text-[14px]"
          />
        </div>

        <div className="press-soft bg-card p-3.5">
          <label className="text-[10.5px] font-bold text-text-muted mb-1.5 block tracking-[0.1em]">账号</label>
          <p className="text-[14px] text-text-body">{account}</p>
        </div>

        {error && <p className="text-[11.5px] text-rust-dark text-center">{error}</p>}
        {saved && <p className="text-[11.5px] text-text-primary text-center font-bold">已保存</p>}

        <button
          onClick={handleSave}
          disabled={saving || !draft.trim() || draft === nickname}
          className="press w-full py-3 text-[14px] font-extrabold tracking-[0.16em] indent-[0.16em] bg-rust text-[#fff5ea] disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {saving ? '保存中…' : '保　存'}
        </button>

        <button
          onClick={handleLogout}
          className="w-full py-3 text-[13.5px] font-bold border-2 border-rust-dark text-rust-dark flex items-center justify-center gap-2 active:bg-rust-dark active:text-page transition-all"
        >
          <LogOut className="w-[16px] h-[16px]" /> 退出登录
        </button>
      </div>
    </ShellPage>
  )
}
