import { useState, useEffect, useCallback, type FormEvent } from 'react'
import { api, isExpectedTokenSyncError } from '@/lib/api'
import { persistTokensToServer } from '@/lib/tokens'
import { setUsername, getUsername } from '@/lib/cookies'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { SectionDivider } from '@/components/shared/SectionDivider'
import { type TokenValidationResult } from '@/components/shared/TokenField'
import { CONTROL_CLASS } from '@/lib/controlStyles'
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
  // Empty until the server answers: the browser keeps no trailer-token copy.
  const [githubToken, setGithubTokenValue] = useState('')
  const [jiraEmail, setJiraEmailValue] = useState('')
  const [jiraToken, setJiraTokenValue] = useState('')
  const [jiraSaToken, setJiraSaTokenValue] = useState('')
  const [validatingGithub, setValidatingGithub] = useState(false)
  const [validatingJira, setValidatingJira] = useState(false)
  const [validatingJiraSa, setValidatingJiraSa] = useState(false)
  const [githubValidation, setGithubValidation] = useState<TokenValidationResult | null>(null)
  const [jiraValidation, setJiraValidation] = useState<TokenValidationResult | null>(null)
  const [jiraSaValidation, setJiraSaValidation] = useState<TokenValidationResult | null>(null)

  const [saving, setSaving] = useState(false)
  const [usernameError, setUsernameError] = useState<string | null>(null)
  const [tokensLoaded, setTokensLoaded] = useState(false)
  /** True once the server actually answered: only then do the fields describe
   *  what is stored, and only then may a save write them back. */
  const [tokensHydrated, setTokensHydrated] = useState(false)
  /** Shown when the account read failed: the fields are unknown, not empty. */
  const [tokensError, setTokensError] = useState<string | null>(null)
  /** Token values as last returned by the server. A save only re-validates what
   *  differs from these, so an untouched stored credential is not re-checked and
   *  a stale browser-cached one is (#294). */
  const [baseline, setBaseline] = useState({ gh: '', je: '', jt: '', jst: '' })

  const hydrateTokensFromServer = useCallback(async () => {
    try {
      const tokens = await api.get<{ github_token: string; jira_email: string; jira_token: string; jira_sa_token: string }>('/api/user/tokens')
      const server = {
        gh: (tokens.github_token || '').trim(),
        je: (tokens.jira_email || '').trim(),
        jt: (tokens.jira_token || '').trim(),
        jst: (tokens.jira_sa_token || '').trim(),
      }
      // The server is the only source: its value is both what the fields show
      // and the baseline a save compares against, so an untouched credential is
      // never re-validated and a stale value cannot win (#294).
      setBaseline(server)
      setTokensHydrated(true)
      setTokensError(null)
      // Only fill a field the user has not typed into yet (empty): a slow read
      // must not throw away an edit started before it landed.
      setGithubTokenValue((prev) => prev || server.gh)
      setJiraEmailValue((prev) => prev || server.je)
      setJiraTokenValue((prev) => prev || server.jt)
      setJiraSaTokenValue((prev) => prev || server.jst)
    } catch (err) {
      // 401 (no cookie yet) and 404 (user not registered) are expected; log anything else.
      if (!isExpectedTokenSyncError(err)) {
        console.error('Failed to hydrate tokens from server:', err)
      }
      setTokensError('Could not read your saved tracker credentials.')
    }
  }, [])

  useEffect(() => {
    if (!initialUsername) {
      setTokensLoaded(true) // no user yet, nothing to load
      return
    }
    hydrateTokensFromServer().finally(() => setTokensLoaded(true))
  // eslint-disable-next-line react-hooks/exhaustive-deps -- initialUsername (lazy useState) and hydrateTokensFromServer (useCallback) are stable; run once on mount
  }, [])

  async function refreshTokensFromServer() {
    await hydrateTokensFromServer()
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

    // A save writes every credential field, empty ones included (#294). After a
    // failed read the fields do not describe what is stored, so write nothing
    // and do not report success — the banner offers the retry instead.
    if (!isViewer && !tokensHydrated) {
      setSaving(false)
      return
    }

    async function commitProfile(trimmedUsername: string) {
      setUsername(trimmedUsername)
      // Nothing is written until the server's own values are known: the fields
      // are empty after a failed read, and sending them would clear every
      // stored credential (#294 clears deliberately, this is not one).
      if (isViewer || !tokensHydrated) return
      // Every field is sent, including empty ones: the server reads an empty
      // field as a clear for that credential (#294).
      const gh = githubToken.trim()
      const je = jiraEmail.trim()
      const jt = jiraToken.trim()
      const jst = jiraSaToken.trim()
      await persistTokensToServer(gh, je, jt, jst)
    }

    // Validate only what this save introduces. A stored token the user never
    // touched is not re-checked, so an already-invalid credential cannot block
    // clearing a different field (#294).
    const needsGithubValidation =
      !!githubToken.trim() && githubToken.trim() !== baseline.gh && (!githubValidation || !githubValidation.valid)
    // Jira authenticates with email + token, so a new pair is worth checking.
    // Clearing the email is not: with no email the backend falls back to bearer
    // auth, so validating that would test a different credential than the one
    // being stored and could block the clear (#294).
    const jiraEmailCleared = !jiraEmail.trim() && baseline.je !== ''
    const needsJiraValidation =
      !!jiraToken.trim() &&
      (jiraToken.trim() !== baseline.jt || (jiraEmail.trim() !== baseline.je && !jiraEmailCleared)) &&
      (!jiraValidation || !jiraValidation.valid)
    // The service account token gets the same pre-save check as the regular
    // Jira credential — a mistyped ATST token must not be saved silently.
    const needsJiraSaValidation =
      !!jiraSaToken.trim() &&
      jiraSaToken.trim() !== baseline.jst &&
      (!jiraSaValidation || !jiraSaValidation.valid)

    if (!isViewer && (needsGithubValidation || needsJiraValidation || needsJiraSaValidation)) {
      const validations = await Promise.allSettled([
        needsGithubValidation ? validateGithub() : Promise.resolve(),
        needsJiraValidation ? validateJira() : Promise.resolve(),
        needsJiraSaValidation ? validateJiraSa() : Promise.resolve(),
      ])

      const results = validations.map((r) => r.status === 'fulfilled' ? r.value : false)
      if (needsGithubValidation && results[0] === false) { setSaving(false); return }
      if (needsJiraValidation && results[1] === false) { setSaving(false); return }
      if (needsJiraSaValidation && results[2] === false) { setSaving(false); return }
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

  function validateJiraSa(): Promise<boolean> {
    // The backend detects the ATST prefix and validates against the scoped
    // Atlassian gateway — no email is involved for service account tokens.
    return validateToken('jira', { token: jiraSaToken.trim() }, setValidatingJiraSa, setJiraSaValidation)
  }

  return (
    <Card className="border-border-muted">
      <CardContent className="p-5">
        <form onSubmit={handleSubmit} className="space-y-4">
          <fieldset disabled={saving || validatingGithub || validatingJira || validatingJiraSa} className="space-y-4">
          {/* Username field */}
          <div className="space-y-1.5">
            <label
              htmlFor={readOnlyUsername ? undefined : 'username'}
              className="block font-display text-xs font-medium uppercase tracking-widest text-text-secondary"
            >
              Username
            </label>
            {readOnlyUsername ? (
              // Read-only stand-in for the Input above: same control shape.
              <div className={`${CONTROL_CLASS} flex items-center px-3`}>
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

          {/* An unsaved edit disables the test button: the endpoint only reads
              the saved row, so testing would silently answer for the old token. */}
          {!isViewer && <TrackerTokensFields
            githubToken={githubToken}
            onGithubTokenChange={(v) => { setGithubTokenValue(v); setGithubValidation(null) }}
            jiraEmail={jiraEmail}
            onJiraEmailChange={(v) => { setJiraEmailValue(v); setJiraValidation(null) }}
            jiraToken={jiraToken}
            onJiraTokenChange={(v) => { setJiraTokenValue(v); setJiraValidation(null) }}
            jiraSaToken={jiraSaToken}
            onJiraSaTokenChange={(v) => { setJiraSaTokenValue(v); setJiraSaValidation(null) }}
            jiraSaValidation={jiraSaValidation}
            githubValidation={githubValidation}
            jiraValidation={jiraValidation}
            newPasswordAutoComplete
            testable
            unsaved={{
              github: githubToken.trim() !== baseline.gh,
              jira:
                jiraEmail.trim() !== baseline.je ||
                jiraToken.trim() !== baseline.jt ||
                jiraSaToken.trim() !== baseline.jst,
            }}
          />}

          {!isViewer && tokensError && (
            <div className="flex items-center justify-between gap-2 rounded-md border border-signal-red/30 bg-signal-red/10 px-3 py-2">
              <p className="text-xs text-signal-red">{tokensError}</p>
              <Button type="button" variant="outline" size="sm" onClick={refreshTokensFromServer}>
                Retry
              </Button>
            </div>
          )}

          <Button type="submit" className="w-full" disabled={(!readOnlyUsername && !username.trim()) || saving || validatingGithub || validatingJira || validatingJiraSa || !tokensLoaded}>
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
