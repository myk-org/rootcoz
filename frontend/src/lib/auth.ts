import { createContext, useContext } from 'react'

export interface AuthState {
  username: string
  isAdmin: boolean
  /** True when the user has operator or admin role. */
  isOperator: boolean
  /** Effective reports access from /me (true for admins; otherwise stored flag). */
  canViewReports: boolean
  canUseServerProviders: boolean
  /** Effective free-pair access (superset: admin role or server grant). */
  canUseFreeProviders: boolean
  role: string
  loading: boolean
  authenticated: boolean
  login: (username: string, apiKey: string) => Promise<void>
  /**
   * End the session and resolve to the IdP logout URL when the provider has a
   * session to end — the caller navigates to it (see `useSignOut`) — or `null`
   * for a purely local logout, which the caller sends to `/login`.
   */
  logout: () => Promise<string | null>
  refreshAuth: () => Promise<void>
}

export const AuthContext = createContext<AuthState | null>(null)

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
