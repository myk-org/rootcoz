import { useState, useEffect, useCallback, useRef, type FormEvent } from 'react'
import { api, isExpectedTokenSyncError } from '@/lib/api'
import { persistTokensToServer } from '@/lib/tokens'
import {
  setUsername,
  setGithubToken,
  setJiraToken,
  setJiraEmail,
  getUsername,
  getGithubToken,
  getJiraToken,
  getJiraEmail,
} from '@/lib/cookies'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { SectionDivider } from '@/components/shared/SectionDivider'
import { type TokenValidationResult } from '@/components/shared/TokenField'
import { TrackerTokensFields } from '@/components/shared/TrackerTokensFields'
import { NotificationToggle } from '@/components/shared/NotificationToggle'

interface ProfileFormProps {
  onSaved: () => void | Promise<void>
  /** When true, the username field is read-only (settings page — user is already authenticated) */
  readOnlyUsername?: boolean
}

export function ProfileForm({ onSaved, readOnlyUsername }: ProfileFormProps) {
  const [initialUsername] = useState(getUsername)
  const [username, setUsernameValue] = useState(initialUsername)
  const [rotating, setRotating] = useState(false)
  const [rotateError, setRotateError] = useState<string | null>(null)
  const [newApiKey, setNewApiKey] = useState('')
  const [keyCopied, setKeyCopied] = useState(false)
  const [githubToken, setGithubTokenValue] = useState(getGithubToken())
  const [jiraEmail, setJiraEmailValue] = useState(getJiraEmail())
  const [jiraToken, setJiraTokenValue] = useState(getJiraToken())
  const [validatingGithub, setValidatingGithub] = useState(false)
  const [validatingJira, setValidatingJira] = useState(false)
  const [githubValidation, setGithubValidation] = useState<TokenValidationResult | null>(null)
  const [jiraValidation, setJiraValidation] = useState<TokenValidationResult | null>(null)

  const [saving, setSaving] = useState(false)
  const [usernameError, setUsernameError] = useState<string | null>(null)
  const [tokensLoaded, setTokensLoaded] = useState(false)

  const githubTokenRef = useRef(githubToken)
  githubTokenRef.current = githubToken
  const jiraEmailRef = useRef(jiraEmail)
  jiraEmailRef.current = jiraEmail
  const jiraTokenRef = useRef(jiraToken)
  jiraTokenRef.current = jiraToken

  const hydrateTokensFromServer = useCallback(
    async (current: { gh: string; je: string; jt: string }) => {
      try {
        const tokens = await api.get<{ github_token: string; jira_email: string; jira_token: string }>('/api/user/tokens')
        if (tokens.github_token && !current.gh.trim()) {
          setGithubTokenValue(tokens.github_token)
          setGithubToken(tokens.github_token)
        }
        if (tokens.jira_email && !current.je.trim()) {
          setJiraEmailValue(tokens.jira_email)
          setJiraEmail(tokens.jira_email)
        }
        if (tokens.jira_token && !current.jt.trim()) {
          setJiraTokenValue(tokens.jira_token)
          setJiraToken(tokens.jira_token)
        }
      } catch (err) {
        // 401 (no cookie yet) and 404 (user not registered) are expected; log anything else.
        if (!isExpectedTokenSyncError(err)) {
          console.error('Failed to hydrate tokens from server:', err)
        }
      }
    },
    [],
  )

  useEffect(() => {
    if (!initialUsername) {
      setTokensLoaded(true) // no user yet, nothing to load
      return
    }
    hydrateTokensFromServer({ gh: githubTokenRef.current, je: jiraEmailRef.current, jt: jiraTokenRef.current }).finally(() => setTokensLoaded(true))
  // eslint-disable-next-line react-hooks/exhaustive-deps -- initialUsername (lazy useState) and hydrateTokensFromServer (useCallback) are stable; run once on mount
  }, [])

  async function refreshTokensFromServer() {
    await hydrateTokensFromServer({ gh: githubToken, je: jiraEmail, jt: jiraToken })
  }

  async function handleRotateKey() {
    setRotating(true)
    setRotateError(null)
    setNewApiKey('')
    try {
      const result = await api.post<{ new_api_key: string }>('/api/auth/rotate-key')
      setNewApiKey(result.new_api_key)
    } catch (err) {
      setRotateError(err instanceof Error ? err.message : 'Failed to rotate key')
    } finally {
      setRotating(false)
    }
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    const trimmed = username.trim()
    if (!readOnlyUsername && !trimmed) return

    setUsernameError(null)
    setSaving(true)

    async function commitProfile(trimmedUsername: string) {
      setUsername(trimmedUsername)
      // Only persist tokens if user actually entered values
      const gh = githubToken.trim()
      const je = jiraEmail.trim()
      const jt = jiraToken.trim()
      setGithubToken(gh)
      setJiraEmail(je)
      setJiraToken(jt)
      await persistTokensToServer(gh, je, jt)
    }

    const needsGithubValidation = githubToken.trim() && (!githubValidation || !githubValidation.valid)
    const needsJiraValidation = jiraToken.trim() && (!jiraValidation || !jiraValidation.valid)

    if (needsGithubValidation || needsJiraValidation) {
      const validations = await Promise.allSettled([
        needsGithubValidation ? validateGithub() : Promise.resolve(),
        needsJiraValidation ? validateJira() : Promise.resolve(),
      ])

      const results = validations.map((r) => r.status === 'fulfilled' ? r.value : false)
      if (needsGithubValidation && results[0] === false) { setSaving(false); return }
      if (needsJiraValidation && results[1] === false) { setSaving(false); return }
    }

    await commitProfile(trimmed)
    // Re-fetch tokens from server before navigating away (onSaved unmounts the component)
    await refreshTokensFromServer()
    setSaving(false)
    await onSaved()
  }

  async function validateToken(
    tokenType: 'github' | 'jira',
    payload: Record<string, string>,
    setValidating: (v: boolean) => void,
    setValidation: (r: TokenValidationResult | null) => void,
  ): Promise<boolean> {
    setValidating(true)
    setValidation(null)
    try {
      const result = await api.post<TokenValidationResult>('/api/validate-token', {
        token_type: tokenType,
        ...payload,
      })
      setValidation(result)
      return result.valid
    } catch {
      setValidation({ valid: false, username: '', message: 'Validation request failed' })
      return false
    } finally {
      setValidating(false)
    }
  }

  function validateGithub(): Promise<boolean> {
    return validateToken('github', { token: githubToken.trim() }, setValidatingGithub, setGithubValidation)
  }

  function validateJira(): Promise<boolean> {
    const email = jiraEmail.trim()
    return validateToken('jira', email ? { token: jiraToken.trim(), email } : { token: jiraToken.trim() }, setValidatingJira, setJiraValidation)
  }

  return (
    <Card className="border-border-muted">
      <CardContent className="p-5">
        <form onSubmit={handleSubmit} className="space-y-4">
          <fieldset disabled={saving || validatingGithub || validatingJira} className="space-y-4">
          {/* Username field */}
          <div className="space-y-1.5">
            <label
              htmlFor={readOnlyUsername ? undefined : 'username'}
              className="block font-display text-xs font-medium uppercase tracking-widest text-text-secondary"
            >
              Username
            </label>
            {readOnlyUsername ? (
              <div className="flex h-10 items-center rounded-md border border-border-default bg-surface-elevated px-3">
                <span className="font-mono text-sm text-text-primary">{username}</span>
              </div>
            ) : (
              <Input
                id="username"
                value={username}
                onChange={(e) => { setUsernameValue(e.target.value); setUsernameError(null) }}
                placeholder="e.g. jdoe"
                autoFocus
                autoComplete="username"
                className="h-10 font-mono"
              />
            )}
            {usernameError && (
              <p className="text-xs text-signal-red">{usernameError}</p>
            )}
          </div>

          {readOnlyUsername && (
            <>
              <SectionDivider title="API Key" />
              {newApiKey ? (
                <div className="space-y-3">
                  <div className="rounded-lg border border-signal-orange/30 bg-signal-orange/10 p-4">
                    <p className="text-sm font-medium text-signal-orange">⚠️ Save this API key — you won't see it again!</p>
                    <p className="mt-1 text-xs text-text-tertiary">Your old key has been invalidated. Use this new key to log in.</p>
                  </div>
                  <div className="flex items-center gap-2 rounded-md bg-surface-elevated p-3">
                    <code className="flex-1 font-mono text-sm text-text-primary break-all select-all">{newApiKey}</code>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={async () => {
                        try {
                          await navigator.clipboard.writeText(newApiKey)
                          setKeyCopied(true)
                          setTimeout(() => setKeyCopied(false), 2000)
                        } catch { /* clipboard not available */ }
                      }}
                    >
                      {keyCopied ? 'Copied!' : 'Copy'}
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="space-y-2">
                  <p className="text-xs text-text-tertiary">
                    Generate a new API key. Your current key and all sessions will be invalidated.
                  </p>
                  <Button type="button" variant="outline" className="w-full" disabled={rotating} onClick={handleRotateKey}>
                    {rotating ? 'Rotating...' : 'Rotate API Key'}
                  </Button>
                  {rotateError && (
                    <p className="text-xs text-signal-red">{rotateError}</p>
                  )}
                </div>
              )}
            </>
          )}

          <TrackerTokensFields
            githubToken={githubToken}
            onGithubTokenChange={(v) => { setGithubTokenValue(v); setGithubValidation(null) }}
            jiraEmail={jiraEmail}
            onJiraEmailChange={(v) => { setJiraEmailValue(v); setJiraValidation(null) }}
            jiraToken={jiraToken}
            onJiraTokenChange={(v) => { setJiraTokenValue(v); setJiraValidation(null) }}
            githubValidation={githubValidation}
            jiraValidation={jiraValidation}
          />

          <Button type="submit" className="w-full" disabled={(!readOnlyUsername && !username.trim()) || saving || validatingGithub || validatingJira || !tokensLoaded}>
            {saving ? 'Saving...' : 'Save'}
          </Button>
          </fieldset>

          {/* Push Notifications */}
          {initialUsername && <NotificationToggle />}

        </form>
      </CardContent>
    </Card>
  )
}
