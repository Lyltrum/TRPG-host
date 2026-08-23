import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { KeyRound, LogOut } from 'lucide-react'
import ShellPage from '@/shared/components/ShellPage'
import { useAuthStore } from '@/stores/auth-store'
import { useRoomStore } from '@/stores/room-store'
import { useCharacterStore } from '@/stores/character-store'
import {
  updateProfile,
  fetchMe,
  changePassword,
  logout as logoutFromServer,
} from '@/services/auth'
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
  const [pwOpen, setPwOpen] = useState(false)
  const [oldPw, setOldPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [newPw2, setNewPw2] = useState('')
  const [pwBusy, setPwBusy] = useState(false)
  const [pwError, setPwError] = useState('')
  const [pwDone, setPwDone] = useState(false)

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

  /**
   * 改密码。
   *
   * 🔴 **两次输入不一致要在前端拦**：后端只收一个新密码，它没法知道你打错了。
   * 🔴 **成功后不跳登录页**：后端只踢掉**其它**会话，当前这条是活的——把做对
   * 事的人踢回登录页是拿一次安全动作去惩罚他。
   */
  const handleChangePassword = async () => {
    if (newPw !== newPw2) {
      setPwError('两次输入的新密码不一样')
      return
    }
    setPwBusy(true)
    setPwError('')
    setPwDone(false)
    try {
      await changePassword(oldPw, newPw)
      setPwDone(true)
      setOldPw('')
      setNewPw('')
      setNewPw2('')
    } catch (err) {
      // 后端那句话要原样给：「原密码不正确」和「新密码不能和原密码一样」
      // 是完全不同的下一步。
      setPwError(friendlyErrorMessage(err, '改不了密码'))
    } finally {
      setPwBusy(false)
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

        {!pwOpen && (
          <button
            onClick={() => { setPwOpen(true); setPwError(''); setPwDone(false) }}
            className="press w-full py-3 text-[13.5px] font-bold border-2 border-text-primary/45 text-text-muted flex items-center justify-center gap-2 active:bg-card transition-all"
          >
            <KeyRound className="w-[16px] h-[16px]" /> 改密码
          </button>
        )}

        {pwOpen && (
          <div className="press-soft bg-card p-3.5 flex flex-col gap-2.5">
            <p className="text-[10.5px] font-bold text-text-muted tracking-[0.1em]">改密码</p>
            <input
              type="password"
              value={oldPw}
              onChange={(e) => { setOldPw(e.target.value); setPwDone(false) }}
              placeholder="原密码"
              className="shell-field w-full px-3 py-2 text-[14px]"
            />
            <input
              type="password"
              value={newPw}
              onChange={(e) => { setNewPw(e.target.value); setPwDone(false) }}
              placeholder="新密码（至少 6 位）"
              className="shell-field w-full px-3 py-2 text-[14px]"
            />
            <input
              type="password"
              value={newPw2}
              onChange={(e) => { setNewPw2(e.target.value); setPwDone(false) }}
              placeholder="再输一遍新密码"
              className="shell-field w-full px-3 py-2 text-[14px]"
            />
            {/* 代价写在按下之前：别的设备会被踢下线 */}
            <p className="text-[11px] text-text-muted leading-relaxed">
              改完之后，<span className="font-bold text-text-primary">别的设备上的登录会失效</span>
              ，这台不受影响。
            </p>
            {pwError && <p className="text-[11.5px] text-rust-dark">{pwError}</p>}
            {pwDone && <p className="text-[11.5px] text-text-primary font-bold">密码已改</p>}
            <div className="flex items-center gap-2">
              <button
                onClick={handleChangePassword}
                disabled={pwBusy || !oldPw || newPw.length < 6 || !newPw2}
                className="press flex-1 py-2.5 text-[13px] font-extrabold bg-rust text-[#fff5ea] disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {pwBusy ? '提交中…' : '确认修改'}
              </button>
              <button
                onClick={() => { setPwOpen(false); setOldPw(''); setNewPw(''); setNewPw2(''); setPwError('') }}
                className="press px-4 py-2.5 text-[13px] font-bold bg-page text-text-muted"
              >
                取消
              </button>
            </div>
          </div>
        )}

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
