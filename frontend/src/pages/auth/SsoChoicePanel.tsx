import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '@/lib/auth'
import { api, ApiError, extractApiDetail } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { TokenField } from '@/components/shared/TokenField'
import { UsernameField } from '@/components/shared/UsernameField'
import { WarningBanner } from './AuthFormPrimitives'

export interface SsoTicket {
  provider: string
  suggested_username: string
  email_domain: string
}

/**
 * First-login screen for an identity no account holds yet.
 *
 * The provider already proved who the person is; the ticket (one-time, bound to
 * this browser) is the only credential these endpoints accept, so the panel
 * asks *which way* to finish and nothing else.
 */
export function SsoChoicePanel({
  ticket,
  ticketValue,
  providerLabel,
  nextPath,
  onExpired,
  onDismiss,
  onCreated,
}: {
  ticket: SsoTicket
  ticketValue: string
  providerLabel: string
  nextPath: string
  onExpired: () => void
  onDismiss: () => void
  /** Called with the new account's one-time API key and where it should land. */
  onCreated: (apiKey: string, destination: string, status: string) => void
}) {
  const navigate = useNavigate()
  const { refreshAuth } = useAuth()
  // Single-step Continue flow: step 1 = username, step 2 = existing account link.
  const [step, setStep] = useState<'username' | 'link'>('username')
  const [username, setUsername] = useState(ticket.suggested_username)
  const [linkName, setLinkName] = useState('')
  const [linkKey, setLinkKey] = useState('')
  const [showLinkKey, setShowLinkKey] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function finish(path: string, body: Record<string, string>) {
    setLoading(true)
    setError('')
    try {
      const result = await api.post<{
        username: string
        status: string
        next?: string
        api_key?: string
      }>(path, body)
      try {
        await refreshAuth()
      } catch {
        // The session cookie is already set — navigate even if /me fails.
      }
      if (result.api_key) {
        onCreated(
          result.api_key,
          result.next || (result.status === 'active' ? nextPath : '/pending'),
          result.status,
        )
        return
      }
      const destination = result.next || (result.status === 'active' ? nextPath : '/pending')
      navigate(destination, { replace: true })
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        onExpired()
        return
      }
      setError(extractApiDetail(err) ?? 'Could not finish signing in. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  /** Step 1: check the username, then create or switch to link step. */
  async function handleContinue() {
    const trimmed = username.trim()
    if (!trimmed) return
    setLoading(true)
    setError('')
    try {
      const result = await api.post<{
        username_exists?: boolean
        provider: string
        suggested_username: string
        email_domain: string
      }>('/api/auth/sso/ticket', { ticket: ticketValue, username: trimmed })
      if (result.username_exists) {
        setLinkName(trimmed)
        setLinkKey('')
        setError('')
        setStep('link')
      } else {
        await finish('/api/auth/sso/create', { ticket: ticketValue, username: trimmed })
        return
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        onExpired()
        return
      }
      setError(extractApiDetail(err) ?? 'Could not finish signing in. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-4">
      <h2 className="font-display text-base font-semibold text-text-primary">
        This {providerLabel} account isn't connected yet
      </h2>
      <WarningBanner message={error} tone="red" />

      {step === 'link' ? (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            const key = linkKey.trim()
            if (linkName && key) {
              void finish('/api/auth/sso/link', {
                ticket: ticketValue,
                username: linkName,
                api_key: key,
              })
            }
          }}
          className="space-y-4"
        >
          <fieldset disabled={loading} className="space-y-4">
            <p className="text-xs text-text-tertiary">
              An account named {linkName} already exists. Enter its API key to
              connect {providerLabel} — no re-approval needed if the account is already active.
            </p>

            <TokenField
              id="sso-link-apikey"
              label="API Key"
              value={linkKey}
              onChange={(v) => { setLinkKey(v); setError('') }}
              show={showLinkKey}
              onToggleShow={() => setShowLinkKey(!showLinkKey)}
              placeholder="Your existing API key..."
              optionalLabel={false}
              autoFocus
            />

            <Button type="submit" className="w-full" disabled={!linkKey.trim() || loading}>
              {loading ? 'Connecting...' : 'Connect account'}
            </Button>

            <p className="text-center text-xs text-text-tertiary">
              <button
                type="button"
                onClick={() => { setStep('username'); setError('') }}
                className="font-medium text-signal-blue hover:underline"
              >
                Use a different username
              </button>
            </p>
          </fieldset>
        </form>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            void handleContinue()
          }}
          className="space-y-4"
        >
          <fieldset disabled={loading} className="space-y-4">
            <div className="space-y-1.5">
              <UsernameField
                id="sso-create-username"
                value={username}
                onChange={(v) => { setUsername(v); setError('') }}
                autoFocus
              />
              <p className="text-xs text-text-tertiary">
                {ticket.email_domain && `Signed in with an @${ticket.email_domain} address. `}
                {username.trim()
                  ? `Enter your existing RootCoz username to link this sign-in, or a new user "${username.trim()}" will be created in RootCoz.`
                  : 'Enter your existing RootCoz username to link this sign-in, or enter a new username to create an account in RootCoz.'}
              </p>
            </div>

            <Button type="submit" className="w-full" disabled={!username.trim() || loading}>
              {loading ? 'Checking...' : 'Continue'}
            </Button>
          </fieldset>
        </form>
      )}

      <p className="text-center text-xs text-text-tertiary">
        <button type="button" onClick={onDismiss} className="font-medium text-signal-blue hover:underline">
          Not you? Sign in another way
        </button>
      </p>
    </div>
  )
}
