// 用户认证状态管理

const TOKEN_KEY = 'claw-auth-token'
const USER_KEY = 'claw-auth-user'

export type AuthUser = {
  id: string
  username: string
}

export function saveAuth(token: string, user: AuthUser) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function loadToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function loadUser(): AuthUser | null {
  try {
    const raw = localStorage.getItem(USER_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}
