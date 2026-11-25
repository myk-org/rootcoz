import { useState, type FormEvent } from 'react'
import { useAuth } from '@/lib/auth'
import { ApiError } from '@/lib/api'
import type { SsoProvider } from '@/lib/ssoProviders'
import { Button } from '@/components/ui/button'
import { TokenField } from '@/components/shared/TokenField'
import { CustomMessageBanner } from '@/components/shared/CustomMessageBanner'
import { UsernameField } from '@/components/shared/UsernameField'
import { SsoProviderButtons, WarningBanner } from './AuthFormPrimitives'

export function LoginForm({
  username,
  onUsernameChange,
  error,
  onErrorChange,
  ssoErrorSentence,
  ssoProviders,
  returnTo,
  onSuccess,
  onSwitchToRegister,
}: {
  username: string
  onUsernameChange: (value: string) => void
  error: string
  onErrorChange: (value: string) => void
  ssoErrorSentence: string
  ssoProviders: SsoProvider[]
  returnTo: string
  onSuccess: () => void
  onSwitchToRegister: () => void
}) {
  const { login } = useAuth()
  const [apiKey, setApiKey] = useState('')
  const [showApiKey, setShowApiKey] = useState(false)
  const [loading, setLoading] = useState(false)
  const [loginCustomMessage, setLoginCustomMessage] = useState('')

  async function handleLogin(e: FormEvent) {
    e.preventDefault()
    const trimmed = username.trim()
    if (!trimmed || !apiKey.trim()) return

    setLoading(true)
    onErrorChange('')
    setLoginCustomMessage('')
    try {
      await login(trimmed, apiKey.trim())
      onSuccess()
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        const body = typeof err.body === 'object' && err.body !== null ? err.body as Record<string, unknown> : {}
        const status = typeof body.status === 'string' ? body.status : ''
        if (err.status === 403 && status === 'pending') {
          const customMsg = typeof body.custom_message === 'string' ? body.custom_message : ''
          onErrorChange('Your account is awaiting admin approval.')
          setLoginCustomMessage(customMsg)
        } else if (err.status === 403 && status === 'rejected') {
          onErrorChange('Your account has been rejected. Contact an admin.')
        } else {
          onErrorChange('Invalid username or API key.')
        }
      } else {
        onErrorChange(err instanceof Error ? err.message : 'Login failed. Check your API key.')
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-4">
      <form onSubmit={handleLogin} className="space-y-4">
        <fieldset disabled={loading} className="space-y-4">
          <WarningBanner message={ssoErrorSentence} />
          <WarningBanner message={error} />

          <CustomMessageBanner message={loginCustomMessage} />

          <UsernameField
            id="login-username"
            value={username}
            onChange={(v) => { onUsernameChange(v); onErrorChange('') }}
            autoFocus={!username}
          />

          <TokenField
            id="login-apikey"
            label="API Key"
            value={apiKey}
            onChange={(v) => { setApiKey(v); onErrorChange('') }}
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
            <button
              type="button"
              onClick={() => { setLoginCustomMessage(''); onSwitchToRegister() }}
              className="text-signal-blue hover:underline font-medium"
            >
              Register
            </button>
          </p>
        </fieldset>
      </form>

      <SsoProviderButtons providers={ssoProviders} returnTo={returnTo} />
    </div>
  )
}
