import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '@/lib/auth'
import { clearTokens, clearUsername } from '@/lib/cookies'

/**
 * End the session, then land on the login form. Without ending it first,
 * `RegisterPage` sees `authenticated` still true and bounces to the dashboard.
 */
export function useSignOut() {
  const { logout } = useAuth()
  const navigate = useNavigate()
  return useCallback(async () => {
    try {
      await logout()
    } catch {
      // The server session is already gone; drop the local copy so the login
      // form is not skipped.
      clearTokens()
      clearUsername()
    }
    navigate('/login')
  }, [logout, navigate])
}
