import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '@/lib/auth'
import { clearTokens, clearUsername } from '@/lib/cookies'
import { hardNavigate } from '@/lib/navigation'

/**
 * End the session, then land on the login form. Without ending it first,
 * `RegisterPage` sees `authenticated` still true and bounces to the dashboard.
 */
export function useSignOut() {
  const { logout } = useAuth()
  const navigate = useNavigate()
  return useCallback(async () => {
    let idpLogoutUrl: string | null = null
    try {
      idpLogoutUrl = await logout()
    } catch {
      // The server session is already gone; drop the local copy so the login
      // form is not skipped.
      clearTokens()
      clearUsername()
    }
    if (idpLogoutUrl && hardNavigate(idpLogoutUrl)) {
      // One navigation, and it is the IdP's: the provider has to end its own
      // session from the browser.
      return
    }
    // No IdP session, or a URL the browser guard refused: the login form is the
    // only place left to be, so a cleared session never strands the user.
    navigate('/login')
  }, [logout, navigate])
}
