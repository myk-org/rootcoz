import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { useSessionState } from '@/lib/useSessionState'
import {
  canManageSignInMethods,
  fetchBrowserSignIn,
  type IdentitiesResponse,
  type SsoProvider,
} from '@/lib/ssoProviders'
import { Button } from '@/components/ui/button'

/** Session-scoped, per-account dismissal: the prompt is a nudge, not a notification. */
const DISMISS_KEY = 'rootcoz-connect-banner-dismissed'

/**
 * A one-line prompt for an account that could connect a provider but has not.
 *
 * Shown only when the deployment offers **browser** SSO **and** the signed-in
 * account has none linked, and only to someone who may act on it.
 */
export function ConnectSignInMethodBanner() {
  const { username } = useAuth()
  if (!username) return null
  return <ConnectBannerForUser key={username} username={username} />
}

function ConnectBannerForUser({ username }: { username: string }) {
  const { authenticated, role } = useAuth()
  const canManage = canManageSignInMethods(role, username, authenticated)
  const [providers, setProviders] = useState<SsoProvider[]>([])
  const [unlinked, setUnlinked] = useState(false)
  const [dismissed, setDismissed] = useSessionState<boolean>(`${DISMISS_KEY}:${username}`, false)

  useEffect(() => {
    if (!canManage) return
    let cancelled = false

    async function load() {
      try {
        const [offered, mine] = await Promise.all([
          fetchBrowserSignIn(),
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
  }, [canManage])

  function dismiss() {
    setDismissed(true)
  }

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
