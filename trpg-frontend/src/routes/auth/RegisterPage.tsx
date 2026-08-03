import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { register } from '@/services/auth'
import { useAuthStore } from '@/stores/auth-store'
import { friendlyErrorMessage } from '@/services/api-client'
import AuthHeader from './AuthHeader'
import ShellPage from '@/shared/components/ShellPage'

// 纯注册页——从 /login 拆出来，独立路由 /login/register。
export default function RegisterPage() {
  const navigate = useNavigate()
  const isLoggedIn = useAuthStore((s) => s.isLoggedIn)
  const authLogin = useAuthStore((s) => s.login)

  const [account, setAccount] = useState('')
  const [password, setPassword] = useState('')
  const [nickname, setNickname] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (isLoggedIn) navigate('/home', { replace: true })
  }, [isLoggedIn, navigate])

  const submit = async () => {
    setError('')
    if (!account.trim() || !password.trim()) {
      setError('请填写账号和密码')
      return
    }
    if (!nickname.trim()) {
      setError('请填写昵称')
      return
    }
    setLoading(true)
    try {
      const res = await register(account.trim(), password, nickname.trim())
      authLogin(res.token, res.userId, nickname.trim())
      navigate('/home')
    } catch (err) {
      setError(friendlyErrorMessage(err, '注册失败'))
    } finally {
      setLoading(false)
    }
  }

  if (isLoggedIn) return null

  return (
    <ShellPage>
      <AuthHeader />

      <div className="px-5 flex flex-col gap-2.5">
        <div className="flex border-2 border-text-primary bg-card mb-1">
          <button
            onClick={() => navigate('/auth/login')}
            className="flex-1 py-2 text-center text-[12.5px] font-bold text-text-primary"
          >
            登录
          </button>
          <span className="flex-1 py-2 text-center text-[12.5px] font-bold bg-text-primary text-page">
            注册
          </span>
        </div>

        <input
          value={account}
          onChange={(e) => setAccount(e.target.value)}
          placeholder="账号"
          className="shell-field w-full px-3 py-2.5 text-[14px]"
        />
        <input
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          type="password"
          placeholder="密码"
          className="shell-field w-full px-3 py-2.5 text-[14px]"
        />
        <input
          value={nickname}
          onChange={(e) => setNickname(e.target.value)}
          placeholder="昵称"
          className="shell-field w-full px-3 py-2.5 text-[14px]"
        />

        {error && <p className="text-[11.5px] text-rust-dark px-1">{error}</p>}

        <button
          onClick={submit}
          disabled={loading}
          className="press w-full py-3 text-[14px] font-extrabold tracking-[0.14em] indent-[0.14em] bg-rust text-[#fff5ea] disabled:opacity-60"
        >
          {loading ? '处理中…' : '注册并进入'}
        </button>
      </div>

      <p className="text-center pt-6 pb-4 text-text-dim text-[11px]">
        AI桌游主持人 © 2026
      </p>
    </ShellPage>
  )
}
