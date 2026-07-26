import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from 'react'
import { api, ApiError } from './api'
import { getUsername, setUsername, getIsAdmin, setIsAdmin, setRole, clearTokens, clearUsername, setGithubToken, setJiraEmail, setJiraToken } from './cookies'
import type { AuthUser } from '@/types'

interface AuthState {
  username: string
  isAdmin: boolean
  /** True when the user has operator or admin role. */
  isOperator: boolean
  /** Effective reports access from /me (true for admins; otherwise stored flag). */
  canViewReports: boolean
  role: string
  loading: boolean
  authenticated: boolean
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
  const [canViewReports, setCanViewReports] = useState(false)
  const [role, setRoleState] = useState('reviewer')
  const [loading, setLoading] = useState(true)
  const [authenticated, setAuthenticated] = useState(false)

  function clearPrivileges() {
    setIsAdminState(false)
    setCanViewReports(false)
    setRoleState('reviewer')
    setIsAdmin(false)
    setRole('reviewer')
    setAuthenticated(false)
  }

  function applyAuthUser(user: AuthUser) {
    setUsernameState(user.username)
    setIsAdminState(user.is_admin)
    setCanViewReports(!!user.can_view_reports)
    setRoleState(user.role)
    setIsAdmin(user.is_admin)
    setRole(user.role)
    if (user.username) {
      setUsername(user.username)
    }
    setAuthenticated(true)
  }

  const refreshAuth = useCallback(async () => {
    try {
      const me = await api.get<AuthUser>('/api/auth/me')
      applyAuthUser(me)
      await syncTokensFromServer(me.username)
    } catch (err) {
      // 401 means not authenticated — clear identity and require login
      if (err instanceof ApiError && err.status === 401) {
        clearPrivileges()
        setUsernameState('')
        clearTokens()
        clearUsername()
      } else {
        // Network/server errors — fall back to cookie identity for display only
        clearPrivileges()
        const cookieUsername = getUsername()
        setUsernameState(cookieUsername)
        await syncTokensFromServer(cookieUsername)
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refreshAuth()
  }, [refreshAuth])

  const login = useCallback(async (loginUsername: string, apiKey: string) => {
    const { resetProviderCatalogCache } = await import('@/lib/useProviderOptions')
    resetProviderCatalogCache()
    const result = await api.post<AuthUser>(
      '/api/auth/login',
      { username: loginUsername, api_key: apiKey }
    )
    // Apply login payload (includes can_view_reports) immediately — do not wait on /me
    applyAuthUser(result)
    await syncTokensFromServer(result.username)
    // Optional /me refresh for other session fields; keep login values if it fails
    try {
      const me = await api.get<AuthUser>('/api/auth/me')
      applyAuthUser(me)
    } catch (err) {
      console.warn(
        'Post-login /api/auth/me refresh failed; keeping login response values:',
        err instanceof Error ? err.message : err,
      )
    }
  }, [])

  const logout = useCallback(async () => {
    try {
      await api.post('/api/auth/logout')
    } catch {
      // ignore
    }
    const { resetProviderCatalogCache } = await import('@/lib/useProviderOptions')
    resetProviderCatalogCache()
    clearPrivileges()
    clearTokens()
    clearUsername()
    // Reset username state to empty
    setUsernameState('')
  }, [])

  return (
    <AuthContext.Provider value={{
      username,
      isAdmin,
      isOperator: role === 'operator' || role === 'admin',
      canViewReports,
      role,
      loading,
      authenticated,
      login,
      logout,
      refreshAuth,
    }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
