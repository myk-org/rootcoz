import { useState, useEffect, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '@/lib/auth'
import { api, ApiError } from '@/lib/api'
import { persistTokensToServer } from '@/lib/tokens'
import { setGithubToken, setJiraEmail, setJiraToken } from '@/lib/cookies'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { TokenField } from '@/components/shared/TokenField'
import { TrackerTokensFields } from '@/components/shared/TrackerTokensFields'
import { NotificationToggle } from '@/components/shared/NotificationToggle'
import { CustomMessageBanner } from '@/components/shared/CustomMessageBanner'

type Mode = 'login' | 'register' | 'key-reveal'
type RegistrationStatus = 'active' | 'pending'

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
  const { login, refreshAuth, authenticated, loading: authLoading } = useAuth()
  const [mode, setMode] = useState<Mode>('login')
  const [username, setUsername] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [showApiKey, setShowApiKey] = useState(false)
  const [newApiKey, setNewApiKey] = useState('')
  const [registrationStatus, setRegistrationStatus] = useState<RegistrationStatus>('active')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [githubToken, setGithubTokenValue] = useState('')
  const [jiraEmail, setJiraEmailValue] = useState('')
  const [jiraToken, setJiraTokenValue] = useState('')
  const [pendingCustomMessage, setPendingCustomMessage] = useState('')
  const [loginCustomMessage, setLoginCustomMessage] = useState('')

  useEffect(() => {
    if (!authLoading && authenticated) {
      navigate('/', { replace: true })
    }
  }, [authLoading, authenticated, navigate])

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
      if (gh) setGithubToken(gh)
      if (je) setJiraEmail(je)
      if (jt) setJiraToken(jt)
      await persistTokensToServer(gh, je, jt)

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
      navigate('/', { replace: true })
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
    navigate('/', { replace: true })
  }

  function switchMode(next: 'login' | 'register') {
    setMode(next)
    setError('')
    setLoginCustomMessage('')
  }

  const subtitle = mode === 'key-reveal'
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
              {mode === 'key-reveal' ? (
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
                    {error && (
                      <div className="rounded-md border border-signal-red/30 bg-signal-red/10 p-3">
                        <p className="text-xs text-signal-red">{error}</p>
                      </div>
                    )}

                    <div className="space-y-1.5">
                      <label htmlFor="reg-username" className="block font-display text-xs font-medium uppercase tracking-widest text-text-secondary">
                        Username
                      </label>
                      <Input
                        id="reg-username"
                        value={username}
                        onChange={(e) => { setUsername(e.target.value); setError('') }}
                        placeholder="e.g. jdoe"
                        autoFocus
                        autoComplete="username"
                        className="h-10 font-mono"
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
                      idPrefix="reg"
                    />

                    <NotificationToggle />

                    <Button type="submit" className="w-full" disabled={!username.trim() || loading}>
                      {loading ? 'Registering...' : 'Register'}
                    </Button>

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
                <form onSubmit={handleLogin} className="space-y-4">
                  <fieldset disabled={loading} className="space-y-4">
                    {error && (
                      <div className="rounded-md border border-signal-amber/30 bg-signal-amber/10 p-3">
                        <p className="text-xs text-signal-amber">{error}</p>
                      </div>
                    )}

                    <CustomMessageBanner message={loginCustomMessage} />

                    <div className="space-y-1.5">
                      <label htmlFor="login-username" className="block font-display text-xs font-medium uppercase tracking-widest text-text-secondary">
                        Username
                      </label>
                      <Input
                        id="login-username"
                        value={username}
                        onChange={(e) => { setUsername(e.target.value); setError('') }}
                        placeholder="e.g. jdoe"
                        autoComplete="username"
                        autoFocus={!username}
                        className="h-10 font-mono"
                      />
                    </div>

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
