import { useRef, useState } from 'react'
import { Loader2, Zap } from 'lucide-react'
import { api, type TestIntegrationResult } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { SectionDivider } from '@/components/shared/SectionDivider'
import { TestResultBanner } from '@/components/shared/TestResultBanner'
import { TokenField, type TokenValidationResult } from '@/components/shared/TokenField'

type Tracker = 'github' | 'jira'

interface TrackerTokensFieldsProps {
  githubToken: string
  onGithubTokenChange: (v: string) => void
  jiraEmail: string
  onJiraEmailChange: (v: string) => void
  jiraToken: string
  onJiraTokenChange: (v: string) => void
  jiraSaToken: string
  onJiraSaTokenChange: (v: string) => void
  /** Service account token validation result (ProfileForm passes it; RegisterPage passes undefined) */
  jiraSaValidation?: TokenValidationResult | null
  /** Token validation results (ProfileForm uses these, RegisterPage passes undefined) */
  githubValidation?: TokenValidationResult | null
  jiraValidation?: TokenValidationResult | null
  /** ID prefix to avoid duplicate IDs when used on multiple pages */
  idPrefix?: string
  /** Opt secret/token fields into new-password autocomplete (management forms only;
   * registration keeps the plain default so browser login autofill is untouched). */
  newPasswordAutoComplete?: boolean
  /** Offer the per-connection test. Off for registration: there is no saved row
   *  to test yet, and the endpoint only ever reads what the server holds. */
  testable?: boolean
  /** A field of this connection was edited and not saved yet, so the button
   *  must not silently test the previously saved token. */
  unsaved?: Partial<Record<Tracker, boolean>>
}

export function TrackerTokensFields({
  githubToken,
  onGithubTokenChange,
  jiraEmail,
  onJiraEmailChange,
  jiraToken,
  onJiraTokenChange,
  jiraSaToken,
  onJiraSaTokenChange,
  jiraSaValidation,
  githubValidation,
  jiraValidation,
  idPrefix = '',
  newPasswordAutoComplete = false,
  testable = false,
  unsaved = {},
}: TrackerTokensFieldsProps) {
  const [showGithubToken, setShowGithubToken] = useState(false)
  const [showJiraToken, setShowJiraToken] = useState(false)
  const [showJiraSaToken, setShowJiraSaToken] = useState(false)
  const [testing, setTesting] = useState<Tracker | null>(null)
  const [testResult, setTestResult] = useState<{ tracker: Tracker; result: TestIntegrationResult } | null>(null)
  // A ref, not the state above: two clicks inside one render must still be one request.
  const inFlight = useRef(false)

  const prefix = idPrefix ? `${idPrefix}-` : ''

  async function handleTest(tracker: Tracker) {
    if (inFlight.current) return
    inFlight.current = true
    setTesting(tracker)
    setTestResult(null)
    try {
      // No body: the endpoint tests the caller's saved tokens, never these fields.
      setTestResult({ tracker, result: await api.testUserTokens(tracker) })
    } catch (err) {
      setTestResult({
        tracker,
        result: { ok: false, message: err instanceof Error ? err.message : 'Test failed' },
      })
    } finally {
      inFlight.current = false
      setTesting(null)
    }
  }

  /** An edit invalidates a result that described the saved token. */
  function dropResult(tracker: Tracker) {
    setTestResult((prev) => (prev?.tracker === tracker ? null : prev))
  }

  function testControls(tracker: Tracker, label: string) {
    if (!testable) return null
    const needsSave = unsaved[tracker] === true
    return (
      <div className="space-y-2">
        <TooltipProvider delayDuration={200}>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="inline-flex">
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="h-7 text-xs"
                  aria-label={`Test ${label} connection`}
                  onClick={() => void handleTest(tracker)}
                  disabled={testing !== null || needsSave}
                >
                  {testing === tracker ? (
                    <><Loader2 className="mr-1 h-3 w-3 animate-spin" />Testing…</>
                  ) : (
                    <><Zap className="mr-1 h-3 w-3" />Test connection</>
                  )}
                </Button>
              </span>
            </TooltipTrigger>
            <TooltipContent>
              {needsSave
                ? 'Save changes before testing — this tests the saved token'
                : 'Tests the saved token, not changes you have not saved'}
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
        {testResult?.tracker === tracker && <TestResultBanner result={testResult.result} className="" />}
      </div>
    )
  }

  return (
    <>
      <SectionDivider title="Tracker Tokens" />
      <p className="text-xs text-text-tertiary">
        Provide your personal tokens to create issues and bugs directly
        under your name. Without tokens, you can still preview generated
        content but cannot submit.
        Clear a field and save to remove that credential from the server; the
        others are left as they are.
      </p>

      {/* GitHub Token field */}
      <TokenField
        id={`${prefix}github-token`}
        label="GitHub Token"
        value={githubToken}
        onChange={(v) => { onGithubTokenChange(v); dropResult('github') }}
        show={showGithubToken}
        onToggleShow={() => setShowGithubToken(!showGithubToken)}
        validation={githubValidation}
        newPasswordAutoComplete={newPasswordAutoComplete}
        placeholder="ghp_..."
        helpContent={<>Personal Access Token with{' '}<code className="text-text-secondary">repo</code> scope.{' '}<a href="https://github.com/settings/tokens" target="_blank" rel="noopener noreferrer" className="text-text-link hover:underline">Generate token →</a></>}
      />

      {testControls('github', 'GitHub')}

      {/* Jira Email field */}
      <TokenField
        id={`${prefix}jira-email`}
        label="Jira Email"
        inputType="email"
        value={jiraEmail}
        onChange={(v) => { onJiraEmailChange(v); dropResult('jira') }}
        placeholder="e.g. jdoe@company.com"
        helpContent="Required for Jira Cloud authentication. Use the same email as your Atlassian account."
      />

      {/* Jira Token field */}
      <TokenField
        id={`${prefix}jira-token`}
        label="Jira Token"
        value={jiraToken}
        onChange={(v) => { onJiraTokenChange(v); dropResult('jira') }}
        show={showJiraToken}
        onToggleShow={() => setShowJiraToken(!showJiraToken)}
        validation={jiraValidation}
        newPasswordAutoComplete={newPasswordAutoComplete}
        placeholder="Token..."
        helpContent={<>Jira Cloud: API token from{' '}<a href="https://id.atlassian.com/manage-profile/security/api-tokens" target="_blank" rel="noopener noreferrer" className="text-text-link hover:underline">Atlassian account →</a>{' '}· Jira Server/DC: Personal Access Token</>}
      />

      {/* Jira Service Account Token field */}
      <TokenField
        id={`${prefix}jira-sa-token`}
        label="Jira Service Account Token"
        value={jiraSaToken}
        onChange={(v) => { onJiraSaTokenChange(v); dropResult('jira') }}
        show={showJiraSaToken}
        onToggleShow={() => setShowJiraSaToken(!showJiraSaToken)}
        validation={jiraSaValidation ?? null}
        newPasswordAutoComplete={newPasswordAutoComplete}
        placeholder="ATST..."
        helpContent={<>Optional Atlassian service account (ATST) token — an additional connection method that takes precedence over the regular Jira token. Created by an admin in Atlassian Administration → Directory → Service accounts.</>}
      />

      {testControls('jira', 'Jira')}
    </>
  )
}
