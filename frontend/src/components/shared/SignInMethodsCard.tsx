import { useCallback, useEffect, useState } from 'react'
import { api, extractApiDetail } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { SSO_LINK_ERROR_FALLBACK, ssoErrorMessage } from '@/lib/ssoErrors'
import {
  canManageSignInMethods,
  fetchBrowserSignIn,
  resolveProviderDisplayName,
  type BrowserSignIn,
  type IdentitiesResponse,
  type LinkedIdentity,
} from '@/lib/ssoProviders'
import { stripQueryParams } from '@/lib/navigation'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { ConfirmDialog } from '@/components/shared/ConfirmDialog'

/** `null` identities cannot tell "never loaded" from "load failed", so the list
 *  has its own state: a failed load must not read as still loading. */
type LoadStatus = 'loading' | 'ready' | 'failed'

/**
 * Connect or disconnect the SSO providers on this account.
 *
 * "Connect" leaves the SPA for the provider and comes back with
 * `?sso_linked=<provider>`, because a redirect exchange cannot happen inside a
 * fetch. It is a real form POST, never a link: a cross-site navigation can follow
 * a GET, so the server accepts `mode=link` only as a same-origin POST.
 */
export function SignInMethodsCard() {
  const { role, username } = useAuth()
  const canManage = canManageSignInMethods(role, username)
  const [status, setStatus] = useState<LoadStatus>('loading')
  const [identities, setIdentities] = useState<LinkedIdentity[] | null>(null)
  const [unlinkable, setUnlinkable] = useState(true)
  const [sso, setSso] = useState<BrowserSignIn>({ providers: [], cliOnly: false })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState('')
  const [linkedNoticeProvider, setLinkedNoticeProvider] = useState('')
  const [confirmProvider, setConfirmProvider] = useState<string | null>(null)

  const labelFor = useCallback(
    (provider: string, list: readonly LinkedIdentity[] | null = identities, offered = sso.providers): string => {
      const matchedIdentity = list?.find((item) => item.provider === provider)
      return resolveProviderDisplayName(provider, offered, matchedIdentity?.display_name)
    },
    [identities, sso.providers],
  )

  const load = useCallback(async () => {
    try {
      const data = await api.get<IdentitiesResponse>('/api/user/identities')
      setIdentities(data.identities)
      setUnlinkable(data.unlinkable)
      setStatus('ready')
    } catch (err) {
      setError(extractApiDetail(err) ?? 'Could not load your sign-in methods.')
      setStatus('failed')
    }
    const offered = await fetchBrowserSignIn()
    setSso(offered)
  }, [])

  useEffect(() => {
    void load()
    // The provider flow redirects back here with the outcome in the query.
    const params = new URLSearchParams(window.location.search)
    const linked = params.get('sso_linked')
    const failed = params.get('sso_error')
    if (linked || failed) {
      if (linked) setLinkedNoticeProvider(linked)
      if (failed) setError(ssoErrorMessage(failed, SSO_LINK_ERROR_FALLBACK))
      // The outcome is a one-shot notice: leaving it in the URL re-shows a stale
      // "connected" (or an old failure) on every refresh and back-navigation.
      stripQueryParams('sso_linked', 'sso_error')
    }
  }, [load])

  async function handleUnlink(provider: string) {
    const displayLabel = labelFor(provider)
    setBusy(provider)
    setError('')
    setNotice('')
    setLinkedNoticeProvider('')
    try {
      await api.delete(`/api/user/identities/${encodeURIComponent(provider)}`)
      setNotice(`${displayLabel} disconnected.`)
      await load()
    } catch (err) {
      setError(extractApiDetail(err) ?? 'That provider could not be disconnected.')
    } finally {
      setBusy('')
    }
  }

  const connected = new Set((identities ?? []).map((identity) => identity.provider))
  // Which providers are already connected is unknown until the list arrives, so
  // no connect button is offered while it is missing or failed.
  const available =
    status === 'ready'
      ? sso.providers.filter((provider) => !connected.has(provider.name))
      : []

  const effectiveNotice = notice || (linkedNoticeProvider ? `${labelFor(linkedNoticeProvider)} connected.` : '')

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

        {status === 'loading' && (
          <p className="text-sm text-text-tertiary">Loading…</p>
        )}

        {status === 'ready' && identities?.length === 0 && (
          <p className="text-sm text-text-tertiary">
            No provider is connected yet — you sign in with your API key.
          </p>
        )}

        {status === 'ready' && identities && identities.length > 0 && (
          <ul className="space-y-2">
            {identities.map((identity) => (
              <li
                key={identity.provider}
                className="flex items-center justify-between gap-2 rounded-md border border-border-default bg-surface-elevated px-3 py-2"
              >
                <div className="min-w-0">
                  <p className="text-sm text-text-primary">
                    {resolveProviderDisplayName(identity.provider, sso.providers, identity.display_name)}
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
                  onClick={() => {
                    if (identities.length === 1 && unlinkable) {
                      setConfirmProvider(identity.provider)
                    } else {
                      void handleUnlink(identity.provider)
                    }
                  }}
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
            another provider or ask an admin to issue an API key first.
          </p>
        )}

        {sso.cliOnly && canManage && (
          <p className="text-xs text-text-tertiary">
            Browser sign-in is turned off on this server, so a provider cannot be
            connected from here. Sign in from a terminal instead:{' '}
            <code className="font-mono text-text-secondary">rootcoz auth sso</code>
          </p>
        )}

        {available.length > 0 && canManage && (
          <div className="flex flex-wrap gap-2">
            {available.map((provider) => (
              <form
                key={provider.name}
                method="post"
                action={`/api/auth/sso/${encodeURIComponent(provider.name)}/login?mode=link&next=${encodeURIComponent('/settings')}`}
              >
                <Button type="submit" variant="outline" size="sm">
                  Connect {provider.display_name}
                </Button>
              </form>
            ))}
          </div>
        )}

        {effectiveNotice && <p className="text-sm text-signal-green">{effectiveNotice}</p>}
        {error && <p className="text-sm text-signal-red">{error}</p>}

        {confirmProvider !== null && (
          <ConfirmDialog
            open
            onOpenChange={(open) => { if (!open) setConfirmProvider(null) }}
            title={`Disconnect ${labelFor(confirmProvider)}?`}
            description="This is your only connected SSO provider. After disconnecting, you will only be able to sign in with your RootCoz API key — make sure you have it saved (or rotate your key in Profile Settings first) so you don’t get locked out."
            confirmLabel="Disconnect"
            variant="destructive"
            loading={busy !== ''}
            onConfirm={() => {
              void handleUnlink(confirmProvider).then(() => setConfirmProvider(null))
            }}
          />
        )}
      </CardContent>
    </Card>
  )
}
