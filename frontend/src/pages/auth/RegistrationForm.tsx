import { useState, type FormEvent } from 'react'
import { api, ApiError } from '@/lib/api'
import { persistTokensToServer } from '@/lib/tokens'
import type { SsoProvider } from '@/lib/ssoProviders'
import { Button } from '@/components/ui/button'
import { TrackerTokensFields } from '@/components/shared/TrackerTokensFields'
import { NotificationToggle } from '@/components/shared/NotificationToggle'
import { UsernameField } from '@/components/shared/UsernameField'
import { SsoProviderButtons, WarningBanner } from './AuthFormPrimitives'

export function RegistrationForm({
  username,
  onUsernameChange,
  error,
  onErrorChange,
  ssoProviders,
  returnTo,
  onRegistered,
  onAlreadyRegistered,
  onSwitchToLogin,
}: {
  username: string
  onUsernameChange: (value: string) => void
  error: string
  onErrorChange: (value: string) => void
  ssoProviders: SsoProvider[]
  returnTo: string
  onRegistered: (apiKey: string, isPending: boolean) => void
  onAlreadyRegistered: () => void
  onSwitchToLogin: () => void
}) {
  const [loading, setLoading] = useState(false)
  const [githubToken, setGithubTokenValue] = useState('')
  const [jiraEmail, setJiraEmailValue] = useState('')
  const [jiraToken, setJiraTokenValue] = useState('')
  const [jiraSaToken, setJiraSaTokenValue] = useState('')

  async function handleRegister(e: FormEvent) {
    e.preventDefault()
    const trimmed = username.trim()
    if (!trimmed) return

    setLoading(true)
    onErrorChange('')
    try {
      const result = await api.post<{ username: string; api_key: string; status?: string }>('/api/auth/register', { username: trimmed })
      const isPending = result.status === 'pending'

      const gh = githubToken.trim()
      const je = jiraEmail.trim()
      const jt = jiraToken.trim()
      const jst = jiraSaToken.trim()
      if (gh || je || jt || jst) await persistTokensToServer(gh, je, jt, jst)

      onRegistered(result.api_key, isPending)
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        const bodyMsg = typeof err.body === 'object' && err.body !== null && 'detail' in err.body
          ? String((err.body as { detail: string }).detail)
          : err.message
        if (/already.*(registered|key|exists|has)/i.test(bodyMsg)) {
          onAlreadyRegistered()
        } else {
          onErrorChange(bodyMsg)
        }
      } else {
        onErrorChange(err instanceof Error ? err.message : 'Registration failed')
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <form onSubmit={handleRegister} className="space-y-4">
      <fieldset disabled={loading} className="space-y-4">
        <WarningBanner message={error} tone="red" />

        <div className="space-y-1.5">
          <UsernameField
            id="reg-username"
            value={username}
            onChange={(v) => { onUsernameChange(v); onErrorChange('') }}
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
          <button type="button" onClick={onSwitchToLogin} className="text-signal-blue hover:underline font-medium">
            Log in
          </button>
        </p>
      </fieldset>
    </form>
  )
}
