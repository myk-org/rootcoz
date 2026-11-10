import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { useSessionState } from '@/lib/useSessionState'
import { Button } from '@/components/ui/button'

/** Session-scoped dismissal: the prompt is a nudge, not a notification. */
const DISMISS_KEY = 'rootcoz-connect-banner-dismissed'

interface IdentitiesResponse {
  identities: { provider: string }[]
}

interface ProvidersResponse {
  providers: { name: string; display_name: string }[]
}

/**
 * A one-line prompt for an account that could connect a provider but has not.
 *
 * Shown only when the deployment offers SSO **and** the signed-in account has
 * none linked, and only to someone who may act on it (a viewer cannot connect
 * one, so telling them would be noise). Dismissing hides it for the session —
 * a permanent dismissal would nag once and then never help anyone who changes
 * their mind; a per-session one is forgotten when the browser is.
 */
export function ConnectSignInMethodBanner() {
  const { authenticated, role } = useAuth()
  const [providers, setProviders] = useState<ProvidersResponse['providers']>([])
  const [unlinked, setUnlinked] = useState(false)
  const [dismissed, setDismissed] = useSessionState<boolean>(DISMISS_KEY, false)

  useEffect(() => {
    // Nothing to offer a viewer, and nothing to load without a session.
    if (!authenticated || role === 'viewer') return
    let cancelled = false

    async function load() {
      try {
        const [offered, mine] = await Promise.all([
          api.get<ProvidersResponse>('/api/auth/sso/providers'),
          api.get<IdentitiesResponse>('/api/user/identities'),
        ])
        if (cancelled) return
        setProviders(offered.providers)
        setUnlinked(mine.identities.length === 0)
      } catch {
        // A deployment without SSO, or a failed probe, is not worth a banner.
        if (!cancelled) setProviders([])
      }
    }

    void load()
    return () => {
      cancelled = true
    }
  }, [authenticated, role])

  function dismiss() {
    setDismissed(true)
  }

  const canManage = authenticated && role !== 'viewer'
  if (!canManage || dismissed || !unlinked || providers.length === 0) return null

  const names = providers.map((provider) => provider.display_name).join(', ')

  return (
    <div className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-md border border-border-default bg-surface-elevated px-3 py-2">
      <p className="text-sm text-text-secondary">
        You sign in with your API key. Connect {names} in{' '}
        <Link to="/settings" className="text-text-link underline">
          Sign-in methods
        </Link>{' '}
        to get in without it.
      </p>
      <Button variant="ghost" size="sm" onClick={dismiss}>
        Dismiss
      </Button>
    </div>
  )
}
