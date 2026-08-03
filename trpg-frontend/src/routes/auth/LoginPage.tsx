import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { login, fetchMe } from '@/services/auth'
import { useAuthStore } from '@/stores/auth-store'
import { friendlyErrorMessage } from '@/services/api-client'
import AuthHeader from './AuthHeader'
import ShellPage from '@/shared/components/ShellPage'

// 纯登录页——注册是另一个路由（/login/register），不再用本地 state 切 tab。
// 登录成功后跳到 /home，那里才是"创建房间/加入房间/浏览已有游戏"这些入口
// 所在的页面。
export default function LoginPage() {
  const navigate = useNavigate()
  const isLoggedIn = useAuthStore((s) => s.isLoggedIn)
  const authLogin = useAuthStore((s) => s.login)

  const [account, setAccount] = useState('')
  const [password, setPassword] = useState('')
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
    setLoading(true)
    try {
      const res = await login(account.trim(), password)
      // 登录接口不返回昵称，得单独查一次 /auth/me 才能拿到真实昵称，
      // 否则会一直显示成账号字符串（见 2026-07-13 测试报告）。
      const me = await fetchMe()
      authLogin(res.token, res.userId, me?.nickname || account.trim())
      navigate('/home')
    } catch (err) {
      setError(friendlyErrorMessage(err, '登录失败'))
    } finally {
      setLoading(false)
    }
  }

  if (isLoggedIn) return null

  return (
    <ShellPage>
      <AuthHeader />

      <div className="px-5 flex flex-col gap-2.5">
        {/* 登录/注册是一个**双联开关**（一整块被墨线切成两半），不是两颗按钮 */}
        <div className="flex border-2 border-text-primary bg-card mb-1">
          <span className="flex-1 py-2 text-center text-[12.5px] font-bold bg-text-primary text-page">
            登录
          </span>
          <button
            onClick={() => navigate('/auth/register')}
            className="flex-1 py-2 text-center text-[12.5px] font-bold text-text-primary"
          >
            注册
          </button>
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

        {error && <p className="text-[11.5px] text-rust-dark px-1">{error}</p>}

        <button
          onClick={submit}
          disabled={loading}
          className="press w-full py-3 text-[14px] font-extrabold tracking-[0.18em] indent-[0.18em] bg-rust text-[#fff5ea] disabled:opacity-60"
        >
          {loading ? '登录中…' : '登　录'}
        </button>
      </div>

      <p className="text-center pt-6 pb-4 text-text-dim text-[11px]">
        AI桌游主持人 © 2026
      </p>
    </ShellPage>
  )
}
