import { createContext, useContext } from 'react'

export interface AuthState {
  username: string
  isAdmin: boolean
  /** True when the user has operator or admin role. */
  isOperator: boolean
  /** Effective reports access from /me (true for admins; otherwise stored flag). */
  canViewReports: boolean
  canUseServerProviders: boolean
  role: string
  loading: boolean
  authenticated: boolean
  login: (username: string, apiKey: string) => Promise<void>
  logout: () => Promise<void>
  refreshAuth: () => Promise<void>
}

export const AuthContext = createContext<AuthState | null>(null)

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
