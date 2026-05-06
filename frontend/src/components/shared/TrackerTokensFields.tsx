import { useState } from 'react'
import { Input } from '@/components/ui/input'
import { SectionDivider } from '@/components/shared/SectionDivider'
import { TokenField, type TokenValidationResult } from '@/components/shared/TokenField'

interface TrackerTokensFieldsProps {
  githubToken: string
  onGithubTokenChange: (v: string) => void
  jiraEmail: string
  onJiraEmailChange: (v: string) => void
  jiraToken: string
  onJiraTokenChange: (v: string) => void
  /** Token validation results (ProfileForm uses these, RegisterPage passes undefined) */
  githubValidation?: TokenValidationResult | null
  jiraValidation?: TokenValidationResult | null
  /** ID prefix to avoid duplicate IDs when used on multiple pages */
  idPrefix?: string
}

export function TrackerTokensFields({
  githubToken,
  onGithubTokenChange,
  jiraEmail,
  onJiraEmailChange,
  jiraToken,
  onJiraTokenChange,
  githubValidation,
  jiraValidation,
  idPrefix = '',
}: TrackerTokensFieldsProps) {
  const [showGithubToken, setShowGithubToken] = useState(false)
  const [showJiraToken, setShowJiraToken] = useState(false)

  const prefix = idPrefix ? `${idPrefix}-` : ''

  return (
    <>
      <SectionDivider title="Tracker Tokens" />
      <p className="text-xs text-text-tertiary">
        Provide your personal tokens to create issues and bugs directly
        under your name. Without tokens, you can still preview generated
        content but cannot submit.
      </p>

      {/* GitHub Token field */}
      <TokenField
        id={`${prefix}github-token`}
        label="GitHub Token"
        value={githubToken}
        onChange={onGithubTokenChange}
        show={showGithubToken}
        onToggleShow={() => setShowGithubToken(!showGithubToken)}
        validation={githubValidation}
        placeholder="ghp_..."
        helpContent={<>Personal Access Token with{' '}<code className="text-text-secondary">repo</code> scope.{' '}<a href="https://github.com/settings/tokens" target="_blank" rel="noopener noreferrer" className="text-text-link hover:underline">Generate token →</a></>}
      />

      {/* Jira Email field */}
      <div className="space-y-1.5">
        <label
          htmlFor={`${prefix}jira-email`}
          className="block font-display text-xs font-medium uppercase tracking-widest text-text-secondary"
        >
          Jira Email{' '}
          <span className="text-text-tertiary font-normal normal-case tracking-normal">
            (optional)
          </span>
        </label>
        <Input
          id={`${prefix}jira-email`}
          type="email"
          value={jiraEmail}
          onChange={(e) => onJiraEmailChange(e.target.value)}
          placeholder="e.g. jdoe@company.com"
          autoComplete="email"
          className="h-10 font-mono"
        />
        <p className="text-xs text-text-tertiary">
          Required for Jira Cloud authentication. Use the same email as
          your Atlassian account.
        </p>
      </div>

      {/* Jira Token field */}
      <TokenField
        id={`${prefix}jira-token`}
        label="Jira Token"
        value={jiraToken}
        onChange={onJiraTokenChange}
        show={showJiraToken}
        onToggleShow={() => setShowJiraToken(!showJiraToken)}
        validation={jiraValidation}
        placeholder="Token..."
        helpContent={<>Jira Cloud: API token from{' '}<a href="https://id.atlassian.com/manage-profile/security/api-tokens" target="_blank" rel="noopener noreferrer" className="text-text-link hover:underline">Atlassian account →</a>{' '}· Jira Server/DC: Personal Access Token</>}
      />
    </>
  )
}
