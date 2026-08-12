import { createContext, useContext, useState, useCallback, type ReactNode } from 'react'
import { loadToken, loadUser, clearAuth, saveAuth } from '../auth'

export type AuthUser = { id: string; username: string }

type AuthContextType = {
  token: string | null
  user: AuthUser | null
  login: (token: string, username: string) => void
  logout: () => void
}

const AuthContext = createContext<AuthContextType | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(loadToken)
  const [user, setUser] = useState<AuthUser | null>(loadUser)

  const login = useCallback((newToken: string, username: string) => {
    const userData = loadUser() || { id: '', username }
    saveAuth(newToken, userData)
    setToken(newToken)
    setUser(userData)
  }, [])

  const logout = useCallback(() => {
    clearAuth()
    setToken(null)
    setUser(null)
  }, [])

  return (
    <AuthContext.Provider value={{ token, user, login, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthContextType {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
