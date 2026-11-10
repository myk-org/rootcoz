import { useCallback, useEffect, useState } from 'react'
import { api, extractApiDetail } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { SSO_LINK_ERROR_FALLBACK, ssoErrorMessage } from '@/lib/ssoErrors'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'

/** One identity as `GET /api/user/identities` reports it. */
interface LinkedIdentity {
  provider: string
  email: string | null
  created_at: string
}

interface IdentitiesResponse {
  identities: LinkedIdentity[]
  unlinkable: boolean
}

interface SsoProvider {
  name: string
  display_name: string
}

const PROVIDER_LABELS: Record<string, string> = {
  google: 'Google',
  github: 'GitHub',
  openshift: 'OpenShift',
  redhat: 'Red Hat SSO',
}

function label(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider
}

/**
 * Connect or disconnect the SSO providers on this account.
 *
 * "Connect" leaves the SPA for the provider and comes back with
 * `?sso_linked=<provider>`, because a redirect exchange cannot happen inside a
 * fetch. The last identity cannot be removed: an account created through SSO is
 * shown its API key once, at creation, and may never have saved it, so unlinking
 * the only way in would lock the person out — the server refuses it and this card
 * says so before the click.
 */
export function SignInMethodsCard() {
  // Connecting and disconnecting are reviewer-level writes on the account, so a
  // viewer is told that instead of being offered a button the server refuses.
  const { role } = useAuth()
  const canManage = role !== 'viewer'
  const [identities, setIdentities] = useState<LinkedIdentity[] | null>(null)
  const [unlinkable, setUnlinkable] = useState(true)
  const [providers, setProviders] = useState<SsoProvider[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    try {
      const data = await api.get<IdentitiesResponse>('/api/user/identities')
      setIdentities(data.identities)
      setUnlinkable(data.unlinkable)
    } catch (err) {
      setError(extractApiDetail(err) ?? 'Could not load your sign-in methods.')
    }
    try {
      const offered = await api.get<{ providers: SsoProvider[] }>(
        '/api/auth/sso/providers',
      )
      setProviders(offered.providers)
    } catch {
      // No SSO on this deployment is not an error worth showing.
      setProviders([])
    }
  }, [])

  useEffect(() => {
    void load()
    // The provider flow redirects back here with the outcome in the query.
    const params = new URLSearchParams(window.location.search)
    const linked = params.get('sso_linked')
    const failed = params.get('sso_error')
    if (linked) setNotice(`${label(linked)} connected.`)
    if (failed) setError(ssoErrorMessage(failed, SSO_LINK_ERROR_FALLBACK))
  }, [load])

  async function handleUnlink(provider: string) {
    setBusy(provider)
    setError('')
    setNotice('')
    try {
      await api.delete(`/api/user/identities/${encodeURIComponent(provider)}`)
      setNotice(`${label(provider)} disconnected.`)
      await load()
    } catch (err) {
      setError(extractApiDetail(err) ?? 'That provider could not be disconnected.')
    } finally {
      setBusy('')
    }
  }

  const connected = new Set((identities ?? []).map((identity) => identity.provider))
  const available = providers.filter((provider) => !connected.has(provider.name))

  return (
    <Card className="mt-4">
      <CardContent className="space-y-3 p-4">
        <div>
          <h2 className="text-sm font-semibold text-text-primary">Sign-in methods</h2>
          <p className="text-xs text-text-tertiary">
            Every provider below signs you in to this same account. Your API key
            works too.
          </p>
        </div>

        {identities === null ? (
          <p className="text-sm text-text-tertiary">Loading…</p>
        ) : identities.length === 0 ? (
          <p className="text-sm text-text-tertiary">
            No provider is connected yet — you sign in with your API key.
          </p>
        ) : (
          <ul className="space-y-2">
            {identities.map((identity) => (
              <li
                key={identity.provider}
                className="flex items-center justify-between gap-2 rounded-md border border-border-default bg-surface-elevated px-3 py-2"
              >
                <div className="min-w-0">
                  <p className="text-sm text-text-primary">
                    {label(identity.provider)}
                  </p>
                  {identity.email && (
                    <p className="truncate text-xs text-text-tertiary">
                      {identity.email}
                    </p>
                  )}
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={busy !== '' || !unlinkable || !canManage}
                  title={
                    !canManage
                      ? 'A reviewer account is required to change sign-in methods.'
                      : unlinkable
                        ? undefined
                        : 'This is your only sign-in method. Connect another one first.'
                  }
                  onClick={() => void handleUnlink(identity.provider)}
                >
                  {busy === identity.provider ? 'Removing…' : 'Disconnect'}
                </Button>
              </li>
            ))}
          </ul>
        )}

        {!canManage && (
          <p className="text-xs text-text-tertiary">
            A reviewer account is required to connect or disconnect a sign-in
            method. Your API key still signs you in.
          </p>
        )}

        {!unlinkable && identities && identities.length > 0 && (
          <p className="text-xs text-text-tertiary">
            This is your only sign-in method, so it cannot be removed. Connect
            another provider, or rotate your API key and keep the new one first.
          </p>
        )}

        {available.length > 0 && canManage && (
          <div className="flex flex-wrap gap-2">
            {available.map((provider) => (
              <Button key={provider.name} variant="outline" size="sm" asChild>
                <a
                  href={`/api/auth/sso/${encodeURIComponent(provider.name)}/login?mode=link&next=${encodeURIComponent('/settings')}`}
                >
                  Connect {provider.display_name}
                </a>
              </Button>
            ))}
          </div>
        )}

        {notice && <p className="text-sm text-signal-green">{notice}</p>}
        {error && <p className="text-sm text-signal-red">{error}</p>}
      </CardContent>
    </Card>
  )
}
