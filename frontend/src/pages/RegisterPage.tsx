import { useState, useEffect, type FormEvent } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useAuth } from '@/lib/auth'
import { api, ApiError, extractApiDetail } from '@/lib/api'
import { SSO_LOGIN_ERROR_FALLBACK, ssoErrorMessage } from '@/lib/ssoErrors'
import { persistTokensToServer } from '@/lib/tokens'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { TokenField } from '@/components/shared/TokenField'
import { TrackerTokensFields } from '@/components/shared/TrackerTokensFields'
import { NotificationToggle } from '@/components/shared/NotificationToggle'
import { CustomMessageBanner } from '@/components/shared/CustomMessageBanner'
import { LoadingSpinner } from '@/components/shared/LoadingSpinner'
import { UsernameField } from '@/components/shared/UsernameField'

type Mode = 'login' | 'register' | 'key-reveal'
type RegistrationStatus = 'active' | 'pending'

interface SsoProvider {
  name: string
  display_name: string
}

interface SsoTicket {
  provider: string
  suggested_username: string
  email_domain: string
}


/** The server's own explanation when it sent one, else a local sentence. */
function failureMessage(err: unknown, fallback: string): string {
  return extractApiDetail(err) ?? fallback
}

/**
 * One full-page sign-in link per configured provider.
 *
 * Rendered on both the login and the register form: the provider list is the
 * deployment's, not the form's, and hiding SSO from people who happen to be
 * registering only makes them pick a weaker credential.
 */
function SsoProviderButtons({ providers, returnTo }: { providers: SsoProvider[]; returnTo: string }) {
  if (providers.length === 0) return null
  return (
    <div className="space-y-2 border-t border-border-muted pt-4">
      {providers.map((provider) => (
        <Button key={provider.name} asChild variant="outline" className="w-full">
          <a href={`/api/auth/sso/${encodeURIComponent(provider.name)}/login?next=${encodeURIComponent(returnTo)}`}>
            Sign in with {provider.display_name}
          </a>
        </Button>
      ))}
    </div>
  )
}

function WarningBanner({ message, tone = 'amber' }: { message: string; tone?: 'amber' | 'red' }) {
  if (!message) return null
  return (
    <div
      // A refusal must be announced, not just coloured: red is an error the
      // person has to act on, amber is a notice.
      role={tone === 'red' ? 'alert' : undefined}
      className={`rounded-md border p-3 ${tone === 'red' ? 'border-signal-red/30 bg-signal-red/10' : 'border-signal-amber/30 bg-signal-amber/10'}`}
    >
      <p className={`text-xs ${tone === 'red' ? 'text-signal-red' : 'text-signal-amber'}`}>{message}</p>
    </div>
  )
}

/**
 * First-login screen for an identity no account holds yet.
 *
 * The provider already proved who the person is; the ticket (one-time, bound to
 * this browser) is the only credential these two endpoints accept, so the panel
 * asks *which way* to finish and nothing else.
 */
function SsoChoicePanel({ ticket, ticketValue, providerLabel, nextPath, onExpired, onDismiss, onCreated }: {
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
  const [createName, setCreateName] = useState(ticket.suggested_username)
  const [linking, setLinking] = useState(false)
  const [linkName, setLinkName] = useState('')
  const [linkKey, setLinkKey] = useState('')
  const [showLinkKey, setShowLinkKey] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  /**
   * Complete the login by posting `body` to `path`.
   *
   * A 404 means the one-time ticket is gone — expired, or already spent by a
   * second submit — so the panel switches to its expired state instead of
   * showing an error the person cannot act on. A `pending` account goes to
   * `/pending` because the middleware refuses it everywhere else.
   */
  async function finish(path: string, body: Record<string, string>) {
    setLoading(true)
    setError('')
    try {
      const result = await api.post<{
        username: string
        status: string
        next?: string
        /** Present only when this call created the account: shown once. */
        api_key?: string
      }>(path, body)
      try {
        await refreshAuth()
      } catch {
        // The session cookie is already set — navigate even if /me fails.
      }
      if (result.api_key) {
        // An account created through SSO has no other credential its owner has
        // ever seen, and the last sign-in method cannot be removed without one —
        // so the key is shown once, exactly like registration.
        onCreated(
          result.api_key,
          result.next || (result.status === 'active' ? nextPath : '/pending'),
          result.status,
        )
        return
      }
      // The server decides where a login ends (pending page, or the destination
      // sealed in the ticket); the SPA only falls back for an older response.
      // The server sends `next`; the fallback mirrors its rule — anything not
      // `active` is blocked (pending *or* rejected), not just pending.
      const destination = result.next || (result.status === 'active' ? nextPath : '/pending')
      navigate(destination, { replace: true })
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        onExpired()
        return
      }
      setError(failureMessage(err, 'Could not finish signing in. Please try again.'))
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

      {linking ? (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            const name = linkName.trim()
            if (name && linkKey.trim()) void finish('/api/auth/sso/link', { ticket: ticketValue, username: name, api_key: linkKey.trim() })
          }}
          className="space-y-4"
        >
          <fieldset disabled={loading} className="space-y-4">
            <p className="text-xs text-text-tertiary">Connect this {providerLabel} sign-in to the account you already have.</p>

            <UsernameField
              id="sso-link-username"
              value={linkName}
              onChange={(v) => { setLinkName(v); setError('') }}
              autoFocus
            />

            <TokenField
              id="sso-link-apikey"
              label="API Key"
              value={linkKey}
              onChange={(v) => { setLinkKey(v); setError('') }}
              show={showLinkKey}
              onToggleShow={() => setShowLinkKey(!showLinkKey)}
              placeholder="Your existing API key..."
              optionalLabel={false}
              autoFocus={!!linkName}
            />

            <Button type="submit" className="w-full" disabled={!linkName.trim() || !linkKey.trim() || loading}>
              {loading ? 'Connecting...' : 'Connect account'}
            </Button>

            <p className="text-center text-xs text-text-tertiary">
              <button
                type="button"
                onClick={() => { setLinking(false); setError('') }}
                className="font-medium text-signal-blue hover:underline"
              >
                Create a new account instead
              </button>
            </p>
          </fieldset>
        </form>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault()
            const name = createName.trim()
            if (name) void finish('/api/auth/sso/create', { ticket: ticketValue, username: name })
          }}
          className="space-y-4"
        >
          <fieldset disabled={loading} className="space-y-4">
            <div className="space-y-1.5">
              <UsernameField
                id="sso-create-username"
                value={createName}
                onChange={(v) => { setCreateName(v); setError('') }}
                autoFocus
              />
              <p className="text-xs text-text-tertiary">
                {ticket.email_domain && `Signed in with an @${ticket.email_domain} address. `}
                You're signed in here. Other devices can use the same provider, and an
                admin can issue an API key for a terminal.
              </p>
            </div>

            <Button type="submit" className="w-full" disabled={!createName.trim() || loading}>
              {loading ? 'Creating account...' : 'Create account'}
            </Button>

            <Button variant="outline" className="w-full" disabled={loading} onClick={() => { setLinking(true); setError('') }}>
              I already have an account
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

function ApiKeyReveal({ apiKey, onAcknowledge }: { apiKey: string; onAcknowledge: () => void }) {
  const [copied, setCopied] = useState(false)

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-signal-orange/30 bg-signal-orange/10 p-4">
        <p className="text-sm font-medium text-signal-orange">⚠️ Save this API key — you won't see it again!</p>
        <p className="mt-1 text-xs text-text-tertiary">You'll need this key to log in. If you lose it, ask an admin to generate a new one.</p>
      </div>
      <div className="flex items-center gap-2 rounded-md bg-surface-elevated p-3">
        <code className="flex-1 font-mono text-sm text-text-primary break-all select-all">{apiKey}</code>
        <Button
          variant="outline"
          size="sm"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(apiKey)
              setCopied(true)
              setTimeout(() => setCopied(false), 2000)
            } catch { /* clipboard not available */ }
          }}
        >
          {copied ? 'Copied!' : 'Copy'}
        </Button>
      </div>
      <Button onClick={onAcknowledge} className="w-full">
        I've saved my key — Continue
      </Button>
    </div>
  )
}

export function RegisterPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const { login, refreshAuth, authenticated, loading: authLoading } = useAuth()
  const from = (location.state as { from?: { pathname: string; search?: string; hash?: string } } | null)?.from
  const returnTo = from ? `${from.pathname}${from.search || ''}${from.hash || ''}` : '/'
  const [mode, setMode] = useState<Mode>('login')
  const [username, setUsername] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [showApiKey, setShowApiKey] = useState(false)
  const [newApiKey, setNewApiKey] = useState('')
  /** Set when this page created an account through SSO: the key is shown once. */
  const [ssoCreated, setSsoCreated] = useState<{ next: string } | null>(null)
  const [registrationStatus, setRegistrationStatus] = useState<RegistrationStatus>('active')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [githubToken, setGithubTokenValue] = useState('')
  const [jiraEmail, setJiraEmailValue] = useState('')
  const [jiraToken, setJiraTokenValue] = useState('')
  const [jiraSaToken, setJiraSaTokenValue] = useState('')
  const [pendingCustomMessage, setPendingCustomMessage] = useState('')
  const [loginCustomMessage, setLoginCustomMessage] = useState('')
  // The callback redirects here as /login#sso_ticket=… — rootcoz, not the IdP.
  // The fragment is never sent with a request, so the ticket stays out of access
  // logs and Referer headers; the panel posts it back to /ticket, /create, /link.
  const ssoTicketValue = new URLSearchParams(location.hash.replace(/^#/, '')).get('sso_ticket') ?? ''
  // Read once: the code is a one-shot notice, so it must not reappear after the
  // person dismisses the panel or switches to Register.
  const [ssoErrorCode] = useState(
    () => new URLSearchParams(location.search).get('sso_error') ?? '',
  )
  const [ssoProviders, setSsoProviders] = useState<SsoProvider[]>([])
  const [ssoTicket, setSsoTicket] = useState<SsoTicket | null>(null)
  const [ssoTicketExpired, setSsoTicketExpired] = useState(false)
  // Derived, not initialised once: the hash is re-read every render, so a ticket
  // that appears without a remount (SPA push, bfcache restore) must not leave an
  // interactive login form on screen while its redemption is in flight.
  const [ssoTicketSettled, setSsoTicketSettled] = useState(ssoTicketValue === '')
  const ssoTicketPending = ssoTicketValue !== '' && !ssoTicketSettled

  useEffect(() => {
    let active = true
    api.get<{ providers?: SsoProvider[]; browser_login?: boolean }>('/api/auth/sso/providers')
      // No public origin means the callback cannot be built, so a button here
      // would only fail after the person clicked it. The CLI still works — it
      // joins a relative verification URL itself.
      .then((data) => { if (active) setSsoProviders(data.browser_login === false ? [] : data.providers ?? []) })
      .catch(() => { /* SSO is optional — the password forms stay usable */ })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (!ssoTicketValue) return
    setSsoTicketSettled(false)
    // POST, never GET: the ticket is a live credential, so it must not travel in
    // a URL — the same reason it arrives in the fragment. `active` keeps a late
    // response from setting state after the panel was dismissed.
    let active = true
    api.post<SsoTicket>('/api/auth/sso/ticket', { ticket: ssoTicketValue })
      .then((ticket) => {
        if (!active) return
        setSsoTicket(ticket)
        // Redeemed: drop it from the address bar and the history entry, so it is
        // not visible for the panel's lifetime (or in a shared screenshot).
        window.history.replaceState(null, '', window.location.pathname)
      })
      .catch((err: unknown) => {
        if (!active) return
        // 404 means the one-time ticket is gone; anything else (a 5xx, a lost
        // binding cookie, the network) is not expiry, and saying so would send
        // the person round the provider again for nothing.
        if (err instanceof ApiError && err.status === 404) {
          setSsoTicketExpired(true)
          return
        }
        setError(failureMessage(err, 'Could not check this sign-in. Please try again.'))
      })
      .finally(() => { if (active) setSsoTicketSettled(true) })
    return () => { active = false }
  }, [ssoTicketValue])

  useEffect(() => {
    // A live session must not swallow an SSO attempt: the person followed a
    // provider link on purpose, so the ticket has to be redeemed first. The
    // key-reveal state is excluded explicitly — a created account flips
    // `authenticated` while its one-time key is still on screen, and the reveal
    // must not be navigated away by the session it just earned.
    if (
      !authLoading &&
      authenticated &&
      !ssoTicketValue &&
      !ssoTicket &&
      !ssoTicketExpired &&
      !ssoCreated &&
      mode !== 'key-reveal'
    ) {
      navigate(returnTo, { replace: true })
    }
  }, [
    authLoading,
    authenticated,
    navigate,
    returnTo,
    ssoTicketValue,
    ssoTicket,
    ssoTicketExpired,
    ssoCreated,
    mode,
  ])

  async function handleRegister(e: FormEvent) {
    e.preventDefault()
    const trimmed = username.trim()
    if (!trimmed) return

    setLoading(true)
    setError('')
    try {
      const result = await api.post<{ username: string; api_key: string; status?: string }>('/api/auth/register', { username: trimmed })
      setNewApiKey(result.api_key)
      const isPending = result.status === 'pending'
      setRegistrationStatus(isPending ? 'pending' : 'active')

      if (isPending) {
        // Fetch custom approval message
        api.get<{ custom_message?: string }>('/api/auth/pending-status')
          .then((data) => { if (data.custom_message) setPendingCustomMessage(data.custom_message) })
          .catch(() => { /* custom message is optional — non-blocking */ })
      }

      // Persist tracker tokens if provided
      const gh = githubToken.trim()
      const je = jiraEmail.trim()
      const jt = jiraToken.trim()
      const jst = jiraSaToken.trim()
      // Only when something was typed: the server reads an empty field as a
      // clear, and a keyless account can already hold tracker credentials from
      // earlier authenticated activity (#294).
      if (gh || je || jt || jst) await persistTokensToServer(gh, je, jt, jst)

      setMode('key-reveal')
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        const bodyMsg = typeof err.body === 'object' && err.body !== null && 'detail' in err.body
          ? String((err.body as { detail: string }).detail)
          : err.message
        if (/already.*(registered|key|exists|has)/i.test(bodyMsg)) {
          setMode('login')
          setError('You already have an API key. Please log in.')
        } else {
          setError(bodyMsg)
        }
      } else {
        setError(err instanceof Error ? err.message : 'Registration failed')
      }
    } finally {
      setLoading(false)
    }
  }

  async function handleLogin(e: FormEvent) {
    e.preventDefault()
    const trimmed = username.trim()
    if (!trimmed || !apiKey.trim()) return

    setLoading(true)
    setError('')
    setLoginCustomMessage('')
    try {
      await login(trimmed, apiKey.trim())
      navigate(returnTo, { replace: true })
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        const body = typeof err.body === 'object' && err.body !== null ? err.body as Record<string, unknown> : {}
        const status = typeof body.status === 'string' ? body.status : ''
        if (err.status === 403 && status === 'pending') {
          const customMsg = typeof body.custom_message === 'string' ? body.custom_message : ''
          setError('Your account is awaiting admin approval.')
          setLoginCustomMessage(customMsg)
        } else if (err.status === 403 && status === 'rejected') {
          setError('Your account has been rejected. Contact an admin.')
        } else {
          setError('Invalid username or API key.')
        }
      } else {
        setError(err instanceof Error ? err.message : 'Login failed. Check your API key.')
      }
    } finally {
      setLoading(false)
    }
  }

  async function handleKeyAcknowledged() {
    if (ssoCreated) {
      // The session is already set, so `/pending` or the ticket's destination is
      // where the person belongs — not the registration page's return path.
      navigate(ssoCreated.next, { replace: true })
      return
    }
    if (registrationStatus === 'pending') {
      // Pending users can't access the app — go to login
      setMode('login')
      return
    }
    try {
      await refreshAuth()
    } catch {
      // Session was set by registration — navigate even if refresh fails
    }
    navigate(returnTo, { replace: true })
  }

  function switchMode(next: 'login' | 'register') {
    setMode(next)
    setError('')
    setLoginCustomMessage('')
  }

  /** Drop the SSO panel and its ticket fragment; the forms come back. */
  function dismissSso() {
    setSsoTicket(null)
    setSsoTicketExpired(false)
    setSsoTicketSettled(true)
    setError('')
    // Drops the fragment as well: the ticket must not survive in the URL.
    navigate(location.pathname, { replace: true })
  }

  const ssoErrorSentence = ssoErrorCode
    ? ssoErrorMessage(ssoErrorCode, SSO_LOGIN_ERROR_FALLBACK)
    : ''
  const ssoProviderLabel = ssoTicket
    ? ssoProviders.find((p) => p.name === ssoTicket.provider)?.display_name ?? ssoTicket.provider
    : ''

  const subtitle = ssoTicketPending || ssoTicket || ssoTicketExpired
    ? 'Finish signing in'
    : mode === 'key-reveal'
      ? 'Your account has been created'
      : mode === 'register'
        ? 'Create your account'
        : 'Log in to continue'

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-surface-page overflow-hidden">
      {/* Ambient grid */}
      <div className="pointer-events-none absolute inset-0 opacity-[0.03]" style={{ backgroundImage: 'linear-gradient(rgba(56,139,253,.4) 1px, transparent 1px), linear-gradient(90deg, rgba(56,139,253,.4) 1px, transparent 1px)', backgroundSize: '48px 48px' }} />
      {/* Radial glow behind card */}
      <div className="pointer-events-none absolute left-1/2 top-1/2 h-[600px] w-[600px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-signal-blue/[0.04] blur-3xl" />

      <div className="relative z-10 w-full max-w-md px-4">
        {/* Logo / Title */}
        <div className="mb-8 animate-slide-up text-center">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-lg border border-border-default bg-surface-card">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" className="text-signal-blue">
              <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <h1 className="font-display text-xl font-bold tracking-tight text-text-primary">RootCoz</h1>
          <p className="mt-1 text-sm text-text-tertiary">{subtitle}</p>
        </div>

        {/* Form card */}
        <div className="animate-slide-up [animation-delay:80ms] [animation-fill-mode:backwards]">
          <Card className="border-border-muted">
            <CardContent className="p-5">
              {ssoTicketPending ? (
                <div className="flex justify-center py-10">
                  <LoadingSpinner />
                </div>
              ) : ssoTicket ? (
                <SsoChoicePanel
                  ticket={ssoTicket}
                  ticketValue={ssoTicketValue}
                  providerLabel={ssoProviderLabel}
                  nextPath={returnTo}
                  onExpired={() => { setSsoTicket(null); setSsoTicketExpired(true) }}
                  onDismiss={dismissSso}
                  onCreated={(apiKey, destination, status) => {
                    // The account exists and the ticket is spent: drop the panel,
                    // or it keeps winning over the reveal below, and drop the
                    // fragment so the spent ticket does not linger in the URL.
                    setSsoTicket(null)
                    window.history.replaceState(null, '', window.location.pathname)
                    setNewApiKey(apiKey)
                    // A pending account sees the same "waiting for approval" note
                    // registration shows, not a bare key followed by a redirect —
                    // including the deployment's own message, which registration
                    // fetches and this path used to skip.
                    const pending = status !== 'active'
                    setRegistrationStatus(pending ? 'pending' : 'active')
                    if (pending) {
                      api
                        .get<{ custom_message?: string }>('/api/auth/pending-status')
                        .then((data) => {
                          if (data.custom_message) setPendingCustomMessage(data.custom_message)
                        })
                        .catch(() => {
                          /* the message is optional — non-blocking */
                        })
                    }
                    setSsoCreated({ next: destination })
                    setMode('key-reveal')
                  }}
                />
              ) : ssoTicketExpired ? (
                <div className="space-y-4">
                  <h2 className="font-display text-base font-semibold text-text-primary">This sign-in link has expired</h2>
                  <p className="text-xs text-text-tertiary">
                    Sign-in links are single use and time limited. Start again with your username and API key.
                  </p>
                  <Button className="w-full" onClick={dismissSso}>Back to sign in</Button>
                </div>
              ) : mode === 'key-reveal' ? (
                <>
                  <h2 className="mb-4 font-display text-base font-semibold text-text-primary">Registration Complete</h2>
                  <ApiKeyReveal apiKey={newApiKey} onAcknowledge={handleKeyAcknowledged} />
                  {registrationStatus === 'pending' && (
                    <div className="mt-4 rounded-md border border-signal-amber/30 bg-signal-amber/10 p-3">
                      <p className="text-xs font-medium text-signal-amber">
                        Your account is pending admin approval. You can save your API key now, but you won't be able to log in until an admin approves your account.
                      </p>
                      <CustomMessageBanner message={pendingCustomMessage} className="mt-2" />
                    </div>
                  )}
                </>
              ) : mode === 'register' ? (
                <form onSubmit={handleRegister} className="space-y-4">
                  <fieldset disabled={loading} className="space-y-4">
                    <WarningBanner message={error} tone="red" />

                    <div className="space-y-1.5">
                      <UsernameField
                        id="reg-username"
                        value={username}
                        onChange={(v) => { setUsername(v); setError('') }}
                        autoFocus
                      />
                      <p className="text-xs text-text-tertiary">Choose a username to create your account. You'll receive an API key for future logins.</p>
                    </div>

                    <TrackerTokensFields
                      githubToken={githubToken}
                      onGithubTokenChange={setGithubTokenValue}
                      jiraEmail={jiraEmail}
                      onJiraEmailChange={setJiraEmailValue}
                      jiraToken={jiraToken}
                      onJiraTokenChange={setJiraTokenValue}
                      jiraSaToken={jiraSaToken}
                      onJiraSaTokenChange={setJiraSaTokenValue}
                      idPrefix="reg"
                    />

                    <NotificationToggle />

                    <Button type="submit" className="w-full" disabled={!username.trim() || loading}>
                      {loading ? 'Registering...' : 'Register'}
                    </Button>

                    <SsoProviderButtons providers={ssoProviders} returnTo={returnTo} />

                    <p className="text-center text-xs text-text-tertiary">
                      Already have an account?{' '}
                      <button type="button" onClick={() => switchMode('login')} className="text-signal-blue hover:underline font-medium">
                        Log in
                      </button>
                    </p>
                  </fieldset>
                </form>
              ) : (
                /* mode === 'login' */
                <div className="space-y-4">
                  <form onSubmit={handleLogin} className="space-y-4">
                    <fieldset disabled={loading} className="space-y-4">
                      <WarningBanner message={ssoErrorSentence} />
                      <WarningBanner message={error} />

                      <CustomMessageBanner message={loginCustomMessage} />

                      <UsernameField
                        id="login-username"
                        value={username}
                        onChange={(v) => { setUsername(v); setError('') }}
                        autoFocus={!username}
                      />

                      <TokenField
                        id="login-apikey"
                        label="API Key"
                        value={apiKey}
                        onChange={(v) => { setApiKey(v); setError('') }}
                        show={showApiKey}
                        onToggleShow={() => setShowApiKey(!showApiKey)}
                        placeholder="Enter your API key..."
                        optionalLabel={false}
                        autoFocus={!!username}
                      />

                      <Button type="submit" className="w-full" disabled={!username.trim() || !apiKey.trim() || loading}>
                        {loading ? 'Logging in...' : 'Log in'}
                      </Button>

                      <p className="text-center text-xs text-text-tertiary">
                        Don't have an account?{' '}
                        <button type="button" onClick={() => switchMode('register')} className="text-signal-blue hover:underline font-medium">
                          Register
                        </button>
                      </p>
                    </fieldset>
                  </form>

                  <SsoProviderButtons providers={ssoProviders} returnTo={returnTo} />
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        <p className="mt-6 animate-slide-up text-center text-xs text-text-tertiary [animation-delay:160ms] [animation-fill-mode:backwards]">
          Your API key authenticates you on every login.<br />
          Tokens are stored locally and synced to the server (encrypted at rest) for cross-browser access.
        </p>
      </div>
    </div>
  )
}
