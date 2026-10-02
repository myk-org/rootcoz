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
import { useAuth } from '@/lib/auth'

interface ProfileFormProps {
  onSaved: () => void | Promise<void>
  /** When true, the username field is read-only (settings page — user is already authenticated) */
  readOnlyUsername?: boolean
}

export function ProfileForm({ onSaved, readOnlyUsername }: ProfileFormProps) {
  const { username: authUsername, role } = useAuth()
  // Tracker tokens are a reviewer capability (server requires reviewer+), so viewers
  // get neither the fields nor any token save/sync path.
  const isViewer = role === 'viewer'
  const [initialUsername] = useState(() => readOnlyUsername && authUsername ? authUsername : getUsername())
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
  /** Token values as last returned by the server. A save only re-validates what
   *  differs from these, so an untouched stored credential is not re-checked and
   *  a stale browser-cached one is (#294). */
  const [baseline, setBaseline] = useState({ gh: '', je: '', jt: '' })

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
        const server = {
          gh: (tokens.github_token || '').trim(),
          je: (tokens.jira_email || '').trim(),
          jt: (tokens.jira_token || '').trim(),
        }
        // Baseline is the server's value, not the browser's: a cookie left over
        // from another browser that has since been replaced counts as changed.
        setBaseline(server)
        if (server.gh && !current.gh.trim()) {
          setGithubTokenValue(server.gh)
          setGithubToken(server.gh)
        }
        if (server.je && !current.je.trim()) {
          setJiraEmailValue(server.je)
          setJiraEmail(server.je)
        }
        if (server.jt && !current.jt.trim()) {
          setJiraTokenValue(server.jt)
          setJiraToken(server.jt)
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
    hydrateTokensFromServer({ gh: githubTokenRef.current, je: jiraEmailRef.current, jt: jiraTokenRef.current })
      .finally(() => setTokensLoaded(true))
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
      if (isViewer) return
      // Every field is sent, including empty ones: the server reads an empty
      // field as a clear for that credential (#294).
      const gh = githubToken.trim()
      const je = jiraEmail.trim()
      const jt = jiraToken.trim()
      setGithubToken(gh)
      setJiraEmail(je)
      setJiraToken(jt)
      await persistTokensToServer(gh, je, jt)
    }

    // Validate only what this save introduces. A stored token the user never
    // touched is not re-checked, so an already-invalid credential cannot block
    // clearing a different field (#294).
    const needsGithubValidation =
      !!githubToken.trim() && githubToken.trim() !== baseline.gh && (!githubValidation || !githubValidation.valid)
    // Jira authenticates with email + token, so either one changing is a change.
    const needsJiraValidation =
      !!jiraToken.trim() &&
      (jiraToken.trim() !== baseline.jt || jiraEmail.trim() !== baseline.je) &&
      (!jiraValidation || !jiraValidation.valid)

    if (!isViewer && (needsGithubValidation || needsJiraValidation)) {
      const validations = await Promise.allSettled([
        needsGithubValidation ? validateGithub() : Promise.resolve(),
        needsJiraValidation ? validateJira() : Promise.resolve(),
      ])

      const results = validations.map((r) => r.status === 'fulfilled' ? r.value : false)
      if (needsGithubValidation && results[0] === false) { setSaving(false); return }
      if (needsJiraValidation && results[1] === false) { setSaving(false); return }
    }

    try {
      await commitProfile(trimmed)
      // Re-fetch tokens from server before navigating away (onSaved unmounts the component)
      await refreshTokensFromServer()
      await onSaved()
    } finally {
      setSaving(false)
    }
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

          {!isViewer && <TrackerTokensFields
            githubToken={githubToken}
            onGithubTokenChange={(v) => { setGithubTokenValue(v); setGithubValidation(null) }}
            jiraEmail={jiraEmail}
            onJiraEmailChange={(v) => { setJiraEmailValue(v); setJiraValidation(null) }}
            jiraToken={jiraToken}
            onJiraTokenChange={(v) => { setJiraTokenValue(v); setJiraValidation(null) }}
            githubValidation={githubValidation}
            jiraValidation={jiraValidation}
          />}

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
