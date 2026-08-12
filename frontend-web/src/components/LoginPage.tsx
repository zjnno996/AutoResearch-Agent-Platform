import { useState } from 'react'
import { saveAuth, loadToken } from '../auth'

const WS_PROTO = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
const AGENT_WS = `${WS_PROTO}//${window.location.host}/ws/agents`

interface Props {
  onLogin: (token: string, username: string) => void
}

type Page = 'login' | 'register'

export default function LoginPage({ onLogin }: Props) {
  const [page, setPage] = useState<Page>('login')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  // 尝试用已有 token 连接
  const tryTokenAuth = () => {
    const token = loadToken()
    if (!token) return false

    setLoading(true)
    const ws = new WebSocket(AGENT_WS)
    ws.onopen = () => {
      ws.send(JSON.stringify({ command: 'auth', token }))
    }
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        if (msg.type === 'auth_result') {
          ws.close()
          setLoading(false)
          if (msg.payload.ok && msg.payload.user) {
            onLogin(token, msg.payload.user.username)
            return
          }
          // Token invalid, clear and show login
          localStorage.removeItem('claw-auth-token')
          localStorage.removeItem('claw-auth-user')
          setError('登录已过期，请重新登录')
        }
      } catch { /* ignore */ }
    }
    ws.onerror = () => { setLoading(false); setError('无法连接服务器') }
    return true
  }

  // Auto-try on mount
  const [tried, setTried] = useState(false)
  if (!tried) {
    setTried(true)
    if (!tryTokenAuth()) {
      setLoading(false)
    }
  }

  const handleSubmit = async () => {
    setError('')
    if (!username.trim() || !password.trim()) {
      setError('请填写用户名和密码')
      return
    }
    setLoading(true)

    try {
      const ws = new WebSocket(AGENT_WS)
      ws.onopen = () => {
        ws.send(JSON.stringify({
          command: page,
          username: username.trim(),
          password,
        }))
      }

      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data)
          if (msg.type === 'auth_result') {
            ws.close()
            if (msg.payload.ok && msg.payload.token) {
              saveAuth(msg.payload.token, msg.payload.user)
              onLogin(msg.payload.token, msg.payload.user.username)
            } else {
              setError(msg.payload.error || '操作失败')
              setLoading(false)
            }
          }
        } catch { /* ignore */ }
      }

      ws.onerror = () => {
        setError('无法连接服务器')
        setLoading(false)
      }

      // Timeout
      setTimeout(() => {
        if (loading) {
          ws.close()
          setError('请求超时')
          setLoading(false)
        }
      }, 10000)
    } catch {
      setError('无法连接服务器')
      setLoading(false)
    }
  }

  const switchPage = () => {
    setPage(page === 'login' ? 'register' : 'login')
    setError('')
  }

  if (loading) {
    return (
      <div className="login-page">
        <div className="login-card">
          <h1>🦞 Claw AI Lab</h1>
          <p className="login-loading">连接中...</p>
        </div>
      </div>
    )
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-logo">🦞</div>
        <h1>Claw AI Lab</h1>
        <p className="login-subtitle">{page === 'login' ? '登录' : '注册'}后开始使用</p>

        {error && <div className="login-error">{error}</div>}

        <div className="login-field">
          <label>用户名</label>
          <input
            type="text"
            placeholder="输入用户名"
            value={username}
            onChange={e => setUsername(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleSubmit()}
            autoFocus
          />
        </div>

        <div className="login-field">
          <label>密码</label>
          <input
            type="password"
            placeholder="输入密码"
            value={password}
            onChange={e => setPassword(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleSubmit()}
          />
        </div>

        <button className="login-btn" onClick={handleSubmit} disabled={!username.trim() || !password.trim()}>
          {page === 'login' ? '登录' : '注册'}
        </button>

        <p className="login-switch">
          {page === 'login' ? (
            <>还没有账号？<a href="#" onClick={(e) => { e.preventDefault(); switchPage() }}>注册</a></>
          ) : (
            <>已有账号？<a href="#" onClick={(e) => { e.preventDefault(); switchPage() }}>登录</a></>
          )}
        </p>
      </div>
    </div>
  )
}
