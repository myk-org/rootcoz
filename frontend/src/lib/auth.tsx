import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from 'react'
import { api, ApiError } from './api'
import { getUsername, setUsername, getIsAdmin, setIsAdmin, setRole, clearTokens, clearUsername, setGithubToken, setJiraEmail, setJiraToken } from './cookies'

interface AuthState {
  username: string
  isAdmin: boolean
  role: string
  loading: boolean
  login: (username: string, apiKey: string) => Promise<void>
  logout: () => Promise<void>
  refreshAuth: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

async function syncTokensFromServer(forUsername: string) {
  if (!forUsername) return
  try {
    const tokens = await api.get<{ github_token: string; jira_email: string; jira_token: string }>('/api/user/tokens')
    setGithubToken(tokens.github_token)
    setJiraEmail(tokens.jira_email)
    setJiraToken(tokens.jira_token)
  } catch {
    // Server tokens not available — keep localStorage values
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [username, setUsernameState] = useState(getUsername())
  const [isAdmin, setIsAdminState] = useState(getIsAdmin())
  const [role, setRoleState] = useState('user')
  const [loading, setLoading] = useState(true)

  const refreshAuth = useCallback(async () => {
    try {
      const me = await api.get<{ username: string; role: string; is_admin: boolean }>('/api/auth/me')
      setUsernameState(me.username)
      setIsAdminState(me.is_admin)
      setRoleState(me.role)
      setIsAdmin(me.is_admin)
      setRole(me.role)
      if (me.username) {
        setUsername(me.username)
      }
      await syncTokensFromServer(me.username)
    } catch {
      // Any error means no valid admin session — fall back to cookie identity
      const cookieUsername = getUsername()
      setUsernameState(cookieUsername)
      setIsAdminState(false)
      setRoleState('user')
      setIsAdmin(false)
      setRole('user')
      await syncTokensFromServer(cookieUsername)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refreshAuth()
  }, [refreshAuth])

  const login = useCallback(async (loginUsername: string, apiKey: string) => {
    const result = await api.post<{ username: string; role: string; is_admin: boolean }>(
      '/api/auth/login',
      { username: loginUsername, api_key: apiKey }
    )
    setUsernameState(result.username)
    setIsAdminState(result.is_admin)
    setRoleState(result.role)
    setUsername(result.username)
    setIsAdmin(result.is_admin)
    setRole(result.role)
  }, [])

  const logout = useCallback(async () => {
    try {
      await api.post('/api/auth/logout')
    } catch {
      // ignore
    }
    setIsAdminState(false)
    setRoleState('user')
    setIsAdmin(false)
    setRole('user')
    clearTokens()
    clearUsername()
    // Reset username state to empty
    setUsernameState('')
  }, [])

  return (
    <AuthContext.Provider value={{ username, isAdmin, role, loading, login, logout, refreshAuth }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
