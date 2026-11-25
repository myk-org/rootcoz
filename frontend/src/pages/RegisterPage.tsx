import { useState, useEffect } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { useAuth } from '@/lib/auth'
import { api, ApiError, extractApiDetail } from '@/lib/api'
import { SSO_LOGIN_ERROR_FALLBACK, ssoErrorMessage } from '@/lib/ssoErrors'
import { clearUrlFragment, stripQueryParams } from '@/lib/navigation'
import {
  fetchBrowserSignIn,
  resolveProviderDisplayName,
  type SsoProvider,
} from '@/lib/ssoProviders'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { LoadingSpinner } from '@/components/shared/LoadingSpinner'
import { WarningBanner } from './auth/AuthFormPrimitives'
import { SsoChoicePanel, type SsoTicket } from './auth/SsoChoicePanel'
import { ApiKeyReveal } from './auth/ApiKeyReveal'
import { LoginForm } from './auth/LoginForm'
import { RegistrationForm } from './auth/RegistrationForm'

type Mode = 'login' | 'register' | 'key-reveal'
type RegistrationStatus = 'active' | 'pending'

interface TicketLookupState {
  key: string
  captured: string
  ticket: { value: string; data: SsoTicket } | null
  expired: boolean
  failure: string
}

const IDLE_TICKET_LOOKUP: TicketLookupState = {
  key: '',
  captured: '',
  ticket: null,
  expired: false,
  failure: '',
}

export function RegisterPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const { refreshAuth, authenticated, loading: authLoading } = useAuth()
  const from = (location.state as { from?: { pathname: string; search?: string; hash?: string } } | null)?.from
  const returnTo = from ? `${from.pathname}${from.search || ''}${from.hash || ''}` : '/'

  const [mode, setMode] = useState<Mode>('login')
  const [username, setUsername] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [showApiKey, setShowApiKey] = useState(false)
  const [githubToken, setGithubTokenValue] = useState('')
  const [jiraEmail, setJiraEmailValue] = useState('')
  const [jiraToken, setJiraTokenValue] = useState('')
  const [jiraSaToken, setJiraSaTokenValue] = useState('')
  const [newApiKey, setNewApiKey] = useState('')
  /** Set when this page created an account through SSO: the key is shown once. */
  const [ssoCreated, setSsoCreated] = useState<{ next: string } | null>(null)
  const [registrationStatus, setRegistrationStatus] = useState<RegistrationStatus>('active')
  const [error, setError] = useState('')
  const [pendingCustomMessage, setPendingCustomMessage] = useState('')

  // The callback redirects here as /login#sso_ticket=… — rootcoz, not the IdP.
  const ssoTicketValue = new URLSearchParams(location.hash.replace(/^#/, '')).get('sso_ticket') ?? ''
  const [ssoErrorCode] = useState(
    () => new URLSearchParams(location.search).get('sso_error') ?? '',
  )
  const [ssoProviders, setSsoProviders] = useState<SsoProvider[]>([])
  const [ssoTicketRetry, setSsoTicketRetry] = useState<{ ticket: string; attempt: number } | null>(null)
  const [ticketLookup, setTicketLookup] = useState<TicketLookupState>(IDLE_TICKET_LOOKUP)

  const activeTicketValue = ssoTicketRetry?.ticket || ssoTicketValue
  const activeTicketKey = ssoTicketRetry
    ? `retry:${ssoTicketRetry.attempt}:${ssoTicketRetry.ticket}`
    : ssoTicketValue
      ? `hash:${ssoTicketValue}`
      : ''

  // Derived pending state: true whenever a live ticket request hasn't settled yet.
  const ssoTicketPending = activeTicketKey !== '' && ticketLookup.key !== activeTicketKey
  const ssoTicketPanel = !ssoTicketPending ? ticketLookup.ticket : null
  const ssoTicketExpired = !ssoTicketPending && ticketLookup.expired
  const ssoTicketFailure = !ssoTicketPending ? ticketLookup.failure : ''

  useEffect(() => {
    let active = true
    void fetchBrowserSignIn().then((data) => {
      if (active) setSsoProviders(data.providers)
    })
    return () => { active = false }
  }, [])

  useEffect(() => {
    stripQueryParams('sso_error')
  }, [])

  useEffect(() => {
    if (!activeTicketValue || !activeTicketKey) return
    let active = true
    api.post<SsoTicket>('/api/auth/sso/ticket', { ticket: activeTicketValue })
      .then((ticket) => {
        if (!active) return
        setTicketLookup({
          key: activeTicketKey,
          captured: activeTicketValue,
          ticket: { value: activeTicketValue, data: ticket },
          expired: false,
          failure: '',
        })
      })
      .catch((err: unknown) => {
        if (!active) return
        if (err instanceof ApiError && err.status === 404) {
          setTicketLookup({
            key: activeTicketKey,
            captured: activeTicketValue,
            ticket: null,
            expired: true,
            failure: '',
          })
          return
        }
        setTicketLookup({
          key: activeTicketKey,
          captured: activeTicketValue,
          ticket: null,
          expired: false,
          failure: extractApiDetail(err) ?? 'Could not check this sign-in. Please try again.',
        })
      })
      .finally(() => {
        if (!active) return
        clearUrlFragment()
      })
    return () => { active = false }
  }, [activeTicketValue, activeTicketKey])

  useEffect(() => {
    if (
      !authLoading &&
      authenticated &&
      !ssoTicketValue &&
      !ssoTicketPending &&
      !ssoTicketPanel &&
      !ssoTicketExpired &&
      !ssoTicketFailure &&
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
    ssoTicketPending,
    ssoTicketPanel,
    ssoTicketExpired,
    ssoTicketFailure,
    ssoCreated,
    mode,
  ])

  function fetchPendingCustomMessage() {
    api
      .get<{ custom_message?: string }>('/api/auth/pending-status')
      .then((data) => {
        if (data.custom_message) setPendingCustomMessage(data.custom_message)
      })
      .catch(() => { /* custom message is optional — non-blocking */ })
  }

  async function handleKeyAcknowledged() {
    if (ssoCreated) {
      navigate(ssoCreated.next, { replace: true })
      return
    }
    if (registrationStatus === 'pending') {
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
  }

  function dismissSso() {
    setTicketLookup((prev) => ({
      ...IDLE_TICKET_LOOKUP,
      key: prev.key,
    }))
    setSsoTicketRetry(null)
    setError('')
    navigate(location.pathname, { replace: true })
  }

  const ssoErrorSentence = ssoErrorCode
    ? ssoErrorMessage(ssoErrorCode, SSO_LOGIN_ERROR_FALLBACK)
    : ''
  const ssoProviderLabel = ssoTicketPanel
    ? resolveProviderDisplayName(ssoTicketPanel.data.provider, ssoProviders)
    : ''

  const subtitle = ssoTicketPending || ssoTicketPanel || ssoTicketFailure || ssoTicketExpired
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
              ) : ssoTicketPanel ? (
                <SsoChoicePanel
                  ticket={ssoTicketPanel.data}
                  ticketValue={ssoTicketPanel.value}
                  providerLabel={ssoProviderLabel}
                  nextPath={returnTo}
                  onExpired={() =>
                    setTicketLookup((prev) => ({ ...prev, ticket: null, expired: true }))
                  }
                  onDismiss={dismissSso}
                  onCreated={(apiKey, destination, status) => {
                    setTicketLookup((prev) => ({
                      ...IDLE_TICKET_LOOKUP,
                      key: prev.key,
                    }))
                    clearUrlFragment()
                    setNewApiKey(apiKey)
                    const pending = status !== 'active'
                    setRegistrationStatus(pending ? 'pending' : 'active')
                    if (pending) fetchPendingCustomMessage()
                    setSsoCreated({ next: destination })
                    setMode('key-reveal')
                  }}
                />
              ) : ssoTicketFailure ? (
                <div className="space-y-4">
                  <h2 className="font-display text-base font-semibold text-text-primary">Couldn't check this sign-in</h2>
                  <WarningBanner message={ssoTicketFailure} tone="red" />
                  <p className="text-xs text-text-tertiary">
                    Your sign-in link is still valid. Try again — no need to go back to
                    your provider.
                  </p>
                  <Button
                    className="w-full"
                    onClick={() =>
                      setSsoTicketRetry((prev) => ({
                        ticket: ticketLookup.captured,
                        attempt: (prev?.attempt ?? 0) + 1,
                      }))
                    }
                  >
                    Try again
                  </Button>
                  <Button variant="outline" className="w-full" onClick={dismissSso}>Back to sign in</Button>
                </div>
              ) : ssoTicketExpired ? (
                <div className="space-y-4">
                  <h2 className="font-display text-base font-semibold text-text-primary">This sign-in link has expired</h2>
                  <p className="text-xs text-text-tertiary">
                    Sign-in links are single use and time limited. Start again with your username and API key.
                  </p>
                  <Button className="w-full" onClick={dismissSso}>Back to sign in</Button>
                </div>
              ) : mode === 'key-reveal' ? (
                <ApiKeyReveal
                  apiKey={newApiKey}
                  pending={registrationStatus === 'pending'}
                  pendingCustomMessage={pendingCustomMessage}
                  onAcknowledge={handleKeyAcknowledged}
                />
              ) : mode === 'register' ? (
                <RegistrationForm
                  username={username}
                  onUsernameChange={setUsername}
                  githubToken={githubToken}
                  onGithubTokenChange={setGithubTokenValue}
                  jiraEmail={jiraEmail}
                  onJiraEmailChange={setJiraEmailValue}
                  jiraToken={jiraToken}
                  onJiraTokenChange={setJiraTokenValue}
                  jiraSaToken={jiraSaToken}
                  onJiraSaTokenChange={setJiraSaTokenValue}
                  error={error}
                  onErrorChange={setError}
                  ssoProviders={ssoProviders}
                  returnTo={returnTo}
                  onRegistered={(createdKey, isPending) => {
                    setNewApiKey(createdKey)
                    setRegistrationStatus(isPending ? 'pending' : 'active')
                    if (isPending) fetchPendingCustomMessage()
                    setMode('key-reveal')
                  }}
                  onAlreadyRegistered={() => {
                    setMode('login')
                    setError('You already have an API key. Please log in.')
                  }}
                  onSwitchToLogin={() => switchMode('login')}
                />
              ) : (
                <LoginForm
                  username={username}
                  onUsernameChange={setUsername}
                  apiKey={apiKey}
                  onApiKeyChange={setApiKey}
                  showApiKey={showApiKey}
                  onToggleShowApiKey={() => setShowApiKey((prev) => !prev)}
                  error={error}
                  onErrorChange={setError}
                  ssoErrorSentence={ssoErrorSentence}
                  ssoProviders={ssoProviders}
                  returnTo={returnTo}
                  onSuccess={() => navigate(returnTo, { replace: true })}
                  onSwitchToRegister={() => switchMode('register')}
                />
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
