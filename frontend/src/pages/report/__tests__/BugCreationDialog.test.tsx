import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { useEffect, type ReactNode } from 'react'
import { ReportProvider } from '../ReportContext'
import { useReportDispatch, useReportState } from '../reportState'
import { BugCreationDialog } from '../BugCreationDialog'
import { resetProviderCatalogCache } from '@/lib/useProviderOptions'
import { ApiError } from '@/lib/api'
import { SERVER_ACCESS_HELP } from '@/components/shared/CredentialSourceLabel'
import type { Capabilities, IssueAttachmentSummary } from '@/types'

HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

const get = vi.fn()
const post = vi.fn()
let grant = true

/** The caller's stored tracker credentials — the dialog's only token source. */
const mockUserTokens = vi.fn(() => ({
  github_token: 'ghp_test_token',
  jira_email: '',
  jira_token: 'jira-tok',
  jira_sa_token: '',
}))

/** A `get` implementation that starts from the account row, so no case has to mock it. */
function withAccountRow(impl: (path: string) => unknown) {
  return async (path: string) =>
    path === '/api/user/tokens' ? mockUserTokens() : impl(path)
}

vi.mock('@/lib/api', () => {
  class _ApiError extends Error {
    status: number
    statusText: string
    body: unknown
    constructor(status: number, statusText: string, body: unknown) {
      super(`API error ${status}: ${statusText}`)
      this.status = status
      this.statusText = statusText
      this.body = body
    }
  }
  return {
    api: { get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args) },
    ApiError: _ApiError,
    extractApiDetail: (err: unknown): string | null => {
      if (!(err instanceof _ApiError)) return null
      const detail = (err.body as { detail?: unknown } | null)?.detail
      if (typeof detail === 'string') return detail
      if (Array.isArray(detail)) {
        const msgs = detail
          .map((item) => (typeof item?.msg === 'string' ? item.msg.replace(/^Value error, /, '') : ''))
          .filter(Boolean)
        return msgs.length > 0 ? msgs.join('; ') : null
      }
      return null
    },
  }
})
vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ role: 'operator', isOperator: true, isAdmin: false, username: 'alice', authenticated: true, canUseServerProviders: grant }),
}))

const catalog = {
  providers: {
    openai: [{ id: 'gpt-4o', name: 'GPT-4o', provider: 'openai', credential_sources: ['user'] }],
    claude: [{ id: 'sonnet', name: 'Sonnet', provider: 'claude', credential_sources: ['server'] }],
  },
  provider_status: {},
}

/** Waits for the issue-prompt fetch to land and the top-of-dialog picker to appear. */
async function openPicker(defaultProvider: string, defaultModel: string) {
  render(
    <MemoryRouter>
      <ReportProvider>
        <BugCreationDialog
          open
          onOpenChange={() => {}}
          jobId="job-1"
          testName="test-a"
          target="github"
          defaultAiProvider={defaultProvider}
          defaultAiModel={defaultModel}
        />
      </ReportProvider>
    </MemoryRouter>,
  )
  await waitFor(() => expect(screen.getByLabelText('Issue Prompt')).toBeInTheDocument())
  expect(screen.getByText('AI for issue generation')).toBeInTheDocument()
}

beforeEach(() => {
  vi.clearAllMocks()
  grant = true
  mockUserTokens.mockReturnValue({
    github_token: 'ghp_test_token',
    jira_email: '',
    jira_token: 'jira-tok',
    jira_sa_token: '',
  })
  get.mockImplementation(withAccountRow((path) => {
    if (path.startsWith('/api/ai-models')) return catalog
    if (path.endsWith('/issue-prompt')) return { issue_prompt: 'Describe the bug' }
    return {}
  }))
  post.mockResolvedValue({ title: 'Issue title', body: 'Issue body', similar_issues: [] })
})

afterEach(() => {
  cleanup()
  localStorage.clear()
  resetProviderCatalogCache()
})

describe('BugCreationDialog AI picker', () => {
  it('seeds the analysis pair and labels every provider option with its credential source', async () => {
    const user = userEvent.setup()
    await openPicker('openai', 'gpt-4o')

    // Default intent: the pair the analysis used, now rendered with its source.
    expect(screen.getByRole('combobox', { name: 'AI Provider' })).toHaveTextContent(/openai.*User/i)
    expect(screen.getByRole('combobox', { name: 'AI Model' })).toHaveValue('gpt-4o')

    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    expect(await screen.findByRole('option', { name: /openai.*User/i })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /claude.*Server/i })).toBeInTheDocument()
  })

  it('gates a server-backed pair when the user has no server grant', async () => {
    grant = false
    const user = userEvent.setup()
    await openPicker('claude', 'sonnet')

    expect(screen.getAllByText(SERVER_ACCESS_HELP).length).toBeGreaterThan(0)
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    const serverModel = await screen.findByRole('option', { name: /sonnet/ })
    expect(serverModel).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByRole('button', { name: /continue/i })).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent(/not available with your credentials/i)

    // A personal-key pair is usable, so the gate clears.
    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    await user.click(await screen.findByRole('option', { name: /openai.*User/i }))
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    await user.click(await screen.findByRole('option', { name: /gpt-4o/ }))
    expect(screen.getByRole('button', { name: /continue/i })).toBeEnabled()
  })

  it('sends the analysis pair with the preview request', async () => {
    const user = userEvent.setup()
    await openPicker('openai', 'gpt-4o')

    await user.click(screen.getByRole('button', { name: /continue/i }))
    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post).toHaveBeenCalledWith('/results/job-1/preview-github-issue',
      expect.objectContaining({ ai_provider: 'openai', ai_model: 'gpt-4o' }))
  })

  it('sends the pair chosen in the dialog', async () => {
    const user = userEvent.setup()
    await openPicker('openai', 'gpt-4o')

    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    await user.click(await screen.findByRole('option', { name: /claude.*Server/i }))
    expect(screen.getByRole('combobox', { name: 'AI Model' })).toHaveValue('')
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    await user.click(await screen.findByRole('option', { name: /sonnet/ }))

    await user.click(screen.getByRole('button', { name: /continue/i }))
    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post).toHaveBeenCalledWith('/results/job-1/preview-github-issue',
      expect.objectContaining({ ai_provider: 'claude', ai_model: 'sonnet' }))
  })

  it('remembers the chosen pair for the next time the dialog opens', async () => {
    const user = userEvent.setup()
    await openPicker('openai', 'gpt-4o')

    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    await user.click(await screen.findByRole('option', { name: /claude.*Server/i }))
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    await user.click(await screen.findByRole('option', { name: /sonnet/ }))
    cleanup()

    // A later dialog on another failure, with a different (empty) analysis pair.
    await openPicker('', '')
    expect(screen.getByRole('combobox', { name: 'AI Provider' })).toHaveTextContent(/claude/i)
    expect(screen.getByRole('combobox', { name: 'AI Model' })).toHaveValue('sonnet')
  })

  it('leaves an empty selection to the server default', async () => {
    const user = userEvent.setup()
    await openPicker('', '')

    // With nothing remembered and no analysis pair, the picker offers the server default.
    expect(screen.getByRole('combobox', { name: 'AI Provider' })).toHaveTextContent(/Server default/i)
    expect(screen.getByRole('button', { name: /continue/i })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: /continue/i }))
    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post).toHaveBeenCalledWith('/results/job-1/preview-github-issue',
      expect.objectContaining({ ai_provider: '', ai_model: '' }))
  })
})

describe('BugCreationDialog Jira pickers', () => {
  it('picks a project from the shared popover list', async () => {
    const user = userEvent.setup()
    post.mockImplementation(async (path: string) => {
      if (path === previewPathFor('jira')) return { title: 'Issue title', body: 'Issue body', similar_issues: [] }
      if (path === '/api/jira-projects') return [{ key: 'MTV', name: 'Migration Toolkit for Virtualization' }]
      if (path === '/api/jira-security-levels') return []
      return {}
    })
    await openGeneratedPreview(user, 'jira')

    await user.type(screen.getByLabelText('Jira Project'), 'mt')
    const list = await screen.findByRole('listbox', { name: 'Jira projects' })
    expect(list).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /MTV/ }))

    expect(screen.queryByRole('listbox')).toBeNull()
    expect(screen.getByLabelText('Jira Project')).toHaveValue('MTV')
  })

  it('keeps the project list open when its input is clicked again', async () => {
    const user = userEvent.setup()
    post.mockImplementation(async (path: string) => {
      if (path === previewPathFor('jira')) return { title: 'Issue title', body: 'Issue body', similar_issues: [] }
      if (path === '/api/jira-projects') return [{ key: 'MTV', name: 'Migration Toolkit for Virtualization' }]
      if (path === '/api/jira-security-levels') return []
      return {}
    })
    await openGeneratedPreview(user, 'jira')

    const input = screen.getByLabelText('Jira Project')
    await user.type(input, 'mt')
    await screen.findByRole('listbox', { name: 'Jira projects' })

    // Clicking the already-focused field to move the caret must not dismiss the list.
    await user.click(input)
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(screen.getByRole('listbox', { name: 'Jira projects' })).toBeInTheDocument()
  })

  // The options are portaled away from the field, so Tab leaves the dialog: the
  // arrows have to reach them, and Enter has to pick the highlighted one.
  it('picks a project with the keyboard while focus stays in the field', async () => {
    const user = userEvent.setup()
    post.mockImplementation(async (path: string) => {
      if (path === previewPathFor('jira')) return { title: 'Issue title', body: 'Issue body', similar_issues: [] }
      if (path === '/api/jira-projects') return [
        { key: 'MTV', name: 'Migration Toolkit for Virtualization' },
        { key: 'MTA', name: 'Migration Toolkit for Applications' },
      ]
      if (path === '/api/jira-security-levels') return []
      return {}
    })
    await openGeneratedPreview(user, 'jira')

    const input = screen.getByLabelText('Jira Project')
    await user.type(input, 'mt')
    await screen.findByRole('listbox', { name: 'Jira projects' })

    await user.keyboard('{ArrowDown}')
    expect(input).toHaveAttribute('aria-activedescendant', 'bug-jira-project-options-option-0')
    await user.keyboard('{ArrowDown}')
    expect(input).toHaveAttribute('aria-activedescendant', 'bug-jira-project-options-option-1')
    await user.keyboard('{ArrowUp}')
    await user.keyboard('{Enter}')

    expect(input).toHaveValue('MTV')
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('picks a security level with the keyboard', async () => {
    const user = userEvent.setup()
    post.mockImplementation(async (path: string) => {
      if (path === previewPathFor('jira')) return { title: 'Issue title', body: 'Issue body', similar_issues: [] }
      if (path === '/api/jira-projects') return [{ key: 'MTV', name: 'Migration Toolkit for Virtualization' }]
      if (path === '/api/jira-security-levels') return [
        { id: '1', name: 'Restricted', description: 'Internal only' },
        { id: '2', name: 'Confidential', description: 'Sensitive' },
      ]
      return {}
    })
    await openGeneratedPreview(user, 'jira')

    await user.type(screen.getByLabelText('Jira Project'), 'mt')
    await screen.findByRole('listbox', { name: 'Jira projects' })
    await user.click(screen.getByRole('button', { name: /MTV/ }))

    const security = screen.getByLabelText('Security Level')
    await user.type(security, 'restr')
    await screen.findByRole('listbox', { name: 'Jira security levels' })
    // The first row is "None (public)", so one arrow down is the first real level.
    await user.keyboard('{ArrowDown}{ArrowDown}{Enter}')

    expect(security).toHaveValue('Restricted')
  })
})

/** Opens the dialog, generates the preview, and leaves it on the Create button. */
async function openGeneratedPreview(user: ReturnType<typeof userEvent.setup>, target: 'github' | 'jira' = 'github') {
  render(
    <MemoryRouter>
      <ReportProvider>
        <BugCreationDialog open onOpenChange={() => {}} jobId="job-1" testName="test-a" target={target} />
      </ReportProvider>
    </MemoryRouter>,
  )
  await waitFor(() => expect(screen.getByLabelText('Issue Prompt')).toBeInTheDocument())
  await user.click(screen.getByRole('button', { name: /continue/i }))
  await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
}

const previewPathFor = (target: 'github' | 'jira') =>
  target === 'github' ? '/results/job-1/preview-github-issue' : '/results/job-1/preview-jira-bug'
const createPathFor = (target: 'github' | 'jira') =>
  target === 'github' ? '/results/job-1/create-github-issue' : '/results/job-1/create-jira-bug'

/** Preview resolves, create fails `failures` times with `failure`, then succeeds. */
function mockCreateFlow(target: 'github' | 'jira', failure: unknown, failures = 1) {
  let attempts = 0
  post.mockImplementation(async (path: string) => {
    if (path === previewPathFor(target)) return { title: 'Issue title', body: 'Issue body', similar_issues: [] }
    if (path === createPathFor(target)) {
      attempts += 1
      if (attempts <= failures) throw failure
      return { url: 'https://tracker/issue/1', key: 'KEY-1', title: 'Issue title', comment_id: 1 }
    }
    return {}
  })
}

describe('BugCreationDialog submit retry', () => {
  it.each(['github', 'jira'] as const)('keeps the generated issue after a failed %s create', async (target) => {
    const user = userEvent.setup()
    mockCreateFlow(target, new ApiError(502, 'Bad Gateway', { detail: 'GitHub is unavailable right now, so your feedback was not filed. Try again in a few minutes.' }))
    await openGeneratedPreview(user, target)

    await user.click(screen.getByRole('button', { name: target === 'github' ? /create github issue/i : /create jira ticket/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'GitHub is unavailable right now, so your feedback was not filed. Try again in a few minutes.',
    )
    expect(screen.getByLabelText('Title')).toHaveValue('Issue title')
    expect(screen.getByLabelText('Body')).toHaveValue('Issue body')
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
  })

  it('Retry resubmits the same payload without regenerating', async () => {
    const user = userEvent.setup()
    mockCreateFlow('github', new ApiError(502, 'Bad Gateway', { detail: 'GitHub is unavailable right now.' }))
    await openGeneratedPreview(user)

    await user.click(screen.getByRole('button', { name: /create github issue/i }))
    await waitFor(() => expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument())

    const before = post.mock.calls.filter(([path]) => path === createPathFor('github'))
    await user.click(screen.getByRole('button', { name: /retry/i }))
    await waitFor(() => expect(screen.getByText('GitHub Issue created successfully.')).toBeInTheDocument())

    expect(post.mock.calls.filter(([path]) => path === previewPathFor('github'))).toHaveLength(1)
    const createCalls = post.mock.calls.filter(([path]) => path === createPathFor('github'))
    expect(createCalls).toHaveLength(before.length + 1)
    expect(createCalls[1][1]).toEqual(createCalls[0][1])
  })

  it('retries a failed Jira create with the same payload', async () => {
    const user = userEvent.setup()
    mockCreateFlow('jira', new ApiError(503, 'Bad Gateway', { detail: 'Jira is unavailable right now.' }))
    await openGeneratedPreview(user, 'jira')

    await user.click(screen.getByRole('button', { name: /create jira ticket/i }))
    await waitFor(() => expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /retry/i }))
    await waitFor(() => expect(screen.getByText('Jira Ticket created successfully.')).toBeInTheDocument())

    expect(post.mock.calls.filter(([path]) => path === previewPathFor('jira'))).toHaveLength(1)
    const createCalls = post.mock.calls.filter(([path]) => path === createPathFor('jira'))
    expect(createCalls).toHaveLength(2)
    expect(createCalls[1][1]).toEqual(createCalls[0][1])
  })

  it('falls back to the generic message when the server sends no detail', async () => {
    const user = userEvent.setup()
    mockCreateFlow('github', new ApiError(502, 'Bad Gateway', {}))
    await openGeneratedPreview(user)

    await user.click(screen.getByRole('button', { name: /create github issue/i }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Failed to create issue')
    // The HTTP status text is never shown to the user.
    expect(alert).not.toHaveTextContent('Bad Gateway')
  })

  it('warns that a 5xx create may already have filed the issue', async () => {
    const user = userEvent.setup()
    mockCreateFlow('github', new ApiError(502, 'Bad Gateway', { detail: 'GitHub did not answer.' }))
    await openGeneratedPreview(user)

    await user.click(screen.getByRole('button', { name: /create github issue/i }))
    expect(await screen.findByRole('status')).toHaveTextContent(
      'the issue may already have been filed. Check GitHub Issue before you retry',
    )
  })

  it('does not warn when the 5xx detail says the issue was not filed', async () => {
    // The server's own outage refusal already tells the user to retry, so an
    // amber "may already have been filed" under it would contradict it.
    const user = userEvent.setup()
    mockCreateFlow('github', new ApiError(502, 'Bad Gateway', {
      detail: 'GitHub is unavailable right now, so your bug report was not filed. Try again in a few minutes.',
    }))
    await openGeneratedPreview(user)

    await user.click(screen.getByRole('button', { name: /create github issue/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'your bug report was not filed. Try again in a few minutes.',
    )
    expect(screen.queryByText(/may already have been filed/i)).not.toBeInTheDocument()
  })

  it('warns when no server answer came back at all', async () => {
    const user = userEvent.setup()
    mockCreateFlow('github', new TypeError('Failed to fetch'))
    await openGeneratedPreview(user)

    await user.click(screen.getByRole('button', { name: /create github issue/i }))
    expect(await screen.findByRole('status')).toHaveTextContent('may already have been filed')
  })

  it('does not warn when a 4xx refusal proves nothing was filed', async () => {
    const user = userEvent.setup()
    mockCreateFlow('github', new ApiError(403, 'Forbidden', { detail: 'GitHub token is missing the repo scope.' }))
    await openGeneratedPreview(user)

    await user.click(screen.getByRole('button', { name: /create github issue/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('missing the repo scope')
    expect(screen.queryByText(/may already have been filed/i)).not.toBeInTheDocument()
  })

  it('adopts an issue filed while the create was failing instead of duplicating it', async () => {
    const user = userEvent.setup()
    mockCreateFlow('github', new ApiError(502, 'Bad Gateway', { detail: 'GitHub is unavailable right now.' }))
    // The POST reports a failure, but the server did file the issue: its
    // tracked-in link appears between the snapshot and the failure.
    let reads = 0
    get.mockImplementation(withAccountRow((path) => {
      if (path.startsWith('/api/ai-models')) return catalog
      if (path.endsWith('/issue-prompt')) return { issue_prompt: 'Describe the bug' }
      if (path.endsWith('/tracked-in')) {
        reads += 1
        return reads === 1 ? { tracked_in: {} } : { tracked_in: { 'test-a': [{ id: 7, tracked_in_url: 'https://tracker/issue/7', tracked_in_type: 'github', tracked_in_by: 'alice' }] } }
      }
      return {}
    }))
    await openGeneratedPreview(user)

    await user.click(screen.getByRole('button', { name: /create github issue/i }))

    expect(await screen.findByText('GitHub Issue created successfully.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /open github issue/i })).toHaveAttribute('href', 'https://tracker/issue/7')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    // No duplicate POST: one create attempt, and the failure was not retried.
    expect(post.mock.calls.filter(([path]) => path === createPathFor('github'))).toHaveLength(1)
  })

  it('never adopts another user\'s issue for the same failure', async () => {
    const user = userEvent.setup()
    mockCreateFlow('github', new ApiError(502, 'Bad Gateway', { detail: 'GitHub is unavailable right now.' }))
    // Bob files an issue for this same failure while our POST fails: the link is
    // scoped to our failure and tracker, but it is not the issue we created.
    let reads = 0
    get.mockImplementation(withAccountRow((path) => {
      if (path.startsWith('/api/ai-models')) return catalog
      if (path.endsWith('/issue-prompt')) return { issue_prompt: 'Describe the bug' }
      if (path.endsWith('/tracked-in')) {
        reads += 1
        return reads === 1 ? { tracked_in: {} } : { tracked_in: { 'test-a': [{ id: 7, tracked_in_url: 'https://tracker/issue/7', tracked_in_type: 'github', tracked_in_by: 'bob' }] } }
      }
      return {}
    }))
    await openGeneratedPreview(user)

    await user.click(screen.getByRole('button', { name: /create github issue/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent('GitHub is unavailable right now.')
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
    expect(screen.queryByText('GitHub Issue created successfully.')).not.toBeInTheDocument()
  })

  it('never reports another failure\'s new link as this create succeeding', async () => {
    const user = userEvent.setup()
    mockCreateFlow('github', new ApiError(502, 'Bad Gateway', { detail: 'GitHub is unavailable right now.' }))
    // Someone else files an issue for a different test while our POST fails.
    get.mockImplementation(withAccountRow((path) => {
      if (path.startsWith('/api/ai-models')) return catalog
      if (path.endsWith('/issue-prompt')) return { issue_prompt: 'Describe the bug' }
      if (path.endsWith('/tracked-in')) {
        return { tracked_in: { 'test-b': [{ id: 9, tracked_in_url: 'https://tracker/issue/9', tracked_in_type: 'github', tracked_in_by: 'bob' }] } }
      }
      return {}
    }))
    await openGeneratedPreview(user)

    await user.click(screen.getByRole('button', { name: /create github issue/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent('GitHub is unavailable right now.')
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
    expect(screen.queryByText('GitHub Issue created successfully.')).not.toBeInTheDocument()
  })

  it('still offers the retry when the tracked-in check itself fails', async () => {
    const user = userEvent.setup()
    mockCreateFlow('github', new ApiError(502, 'Bad Gateway', { detail: 'GitHub is unavailable right now.' }))
    get.mockImplementation(withAccountRow((path) => {
      if (path.startsWith('/api/ai-models')) return catalog
      if (path.endsWith('/issue-prompt')) return { issue_prompt: 'Describe the bug' }
      if (path.endsWith('/tracked-in')) throw new Error('tracked-in unavailable')
      return {}
    }))
    await openGeneratedPreview(user)

    await user.click(screen.getByRole('button', { name: /create github issue/i }))

    // The create still runs and the failure is reported, not left spinning.
    expect(await screen.findByRole('alert')).toHaveTextContent('GitHub is unavailable right now.')
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
    expect(post.mock.calls.filter(([path]) => path === createPathFor('github'))).toHaveLength(1)
  })

  it('double-clicking Retry submits once', async () => {
    const user = userEvent.setup()
    mockCreateFlow('github', new ApiError(502, 'Bad Gateway', { detail: 'GitHub is unavailable right now.' }))
    await openGeneratedPreview(user)

    await user.click(screen.getByRole('button', { name: /create github issue/i }))
    await waitFor(() => expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument())

    const retryButton = screen.getByRole('button', { name: /retry/i })
    fireEvent.click(retryButton)
    fireEvent.click(retryButton)
    await waitFor(() => expect(screen.getByText('GitHub Issue created successfully.')).toBeInTheDocument())

    // One failed create + exactly one retry.
    expect(post.mock.calls.filter(([path]) => path === createPathFor('github'))).toHaveLength(2)
  })
})

describe('BugCreationDialog similar issues', () => {
  it('lists similar issues through the shared match list', async () => {
    const user = userEvent.setup()
    post.mockResolvedValue({
      title: 'Issue title',
      body: 'Issue body',
      similar_issues: [
        { number: 42, key: '', title: 'Login 500', url: 'https://github.com/org/repo/issues/42', status: 'open' },
      ],
    })
    await openGeneratedPreview(user)

    expect(screen.getByText('1 similar issue found')).toBeInTheDocument()
    const link = screen.getByText('#42: Login 500').closest('a')
    expect(link).toHaveAttribute('href', 'https://github.com/org/repo/issues/42')
    expect(screen.getByText('open')).toBeInTheDocument()
  })
})

describe('BugCreationDialog tracker credential', () => {
  const CREDENTIAL_KEYS = ['github_token', 'jira_token', 'jira_email', 'jira_sa_token']

  it('sends no tracker credential in the preview or the create payload', async () => {
    const user = userEvent.setup()
    await openGeneratedPreview(user)
    await user.click(screen.getByRole('button', { name: /create github issue/i }))
    await waitFor(() => expect(screen.getByText('GitHub Issue created successfully.')).toBeInTheDocument())

    const trackerBodies = post.mock.calls
      .filter(([path]) => path === previewPathFor('github') || path === createPathFor('github'))
      .map(([, body]) => body as Record<string, unknown>)
    expect(trackerBodies).toHaveLength(2)
    for (const body of trackerBodies) {
      for (const key of CREDENTIAL_KEYS) expect(body).not.toHaveProperty(key)
    }
  })

  it.each([
    ['github', '/results/job-1/preview-github-issue'],
    ['jira', '/results/job-1/preview-jira-bug'],
  ] as const)(
    'disables Create and points at Profile Settings when the row holds no %s token',
    async (target, previewPath) => {
      mockUserTokens.mockReturnValue({ github_token: '', jira_email: '', jira_token: '', jira_sa_token: '' })
      const user = userEvent.setup()
      await openGeneratedPreview(user, target)

      expect(post.mock.calls.filter(([path]) => path === previewPath)).toHaveLength(1)
      expect(await screen.findByRole('link', { name: /Open Profile Settings/ })).toBeInTheDocument()
      expect(
        screen.getByRole('button', { name: target === 'github' ? /create github issue/i : /create jira ticket/i }),
      ).toBeDisabled()
    },
  )

  it('keeps Create usable when the credential read fails', async () => {
    get.mockImplementation(async (path) => {
      if (path === '/api/user/tokens') throw new Error('server unavailable')
      if (path.startsWith('/api/ai-models')) return catalog
      if (path.endsWith('/issue-prompt')) return { issue_prompt: 'Describe the bug' }
      return {}
    })
    const user = userEvent.setup()
    await openGeneratedPreview(user)

    // A failed read leaves the row unknown, not empty: the server's own refusal
    // names the fix, while a disabled button leaves no way to try.
    expect(screen.getByRole('button', { name: /create github issue/i })).toBeEnabled()
    expect(screen.queryByRole('link', { name: /Open Profile Settings/ })).not.toBeInTheDocument()
  })

  it('enables Create when the row holds a token', async () => {
    const user = userEvent.setup()
    await openGeneratedPreview(user)
    expect(screen.getByRole('button', { name: /create github issue/i })).toBeEnabled()
    expect(screen.queryByRole('link', { name: /Open Profile Settings/ })).not.toBeInTheDocument()
  })
})

function makeAttachmentCapabilities(
  overrides?: Partial<Capabilities['issue_attachments']>,
): Capabilities {
  return {
    github_issues_enabled: true,
    jira_issues_enabled: true,
    issue_attachments: {
      max_files: 3,
      max_file_bytes: 5242880,
      max_total_bytes: 10485760,
      types: ['text/plain', 'image/png', 'image/jpeg', 'image/gif', 'image/webp'],
      report_github: { available: true, reason: 'available' },
      report_jira: { available: true, reason: 'available' },
      feedback: { available: true, reason: 'available' },
      chat_ui: { available: false, reason: 'browser_chat_unsupported' },
      direct_chat: { available: true, reason: 'available' },
      ...overrides,
    },
  }
}

async function openPreviewWithCapabilities(
  user: ReturnType<typeof userEvent.setup>,
  target: 'github' | 'jira',
  capabilities?: Capabilities | null,
) {
  const view = render(
    <MemoryRouter>
      <ReportProvider>
        <BugCreationDialog
          open
          onOpenChange={() => {}}
          jobId="job-1"
          testName="test-a"
          target={target}
          capabilities={capabilities}
        />
      </ReportProvider>
    </MemoryRouter>,
  )
  await waitFor(() => expect(screen.getByLabelText('Issue Prompt')).toBeInTheDocument())
  await user.click(screen.getByRole('button', { name: /continue/i }))
  await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
  return view
}

describe('BugCreationDialog issue attachments', () => {
  afterEach(() => vi.restoreAllMocks())

  it('disables the picker while capabilities are pending or unavailable', async () => {
    const user = userEvent.setup()
    get.mockImplementation(withAccountRow((path) => {
      if (path === '/api/capabilities') return new Promise(() => {})
      if (path.startsWith('/api/ai-models')) return catalog
      if (path.endsWith('/issue-prompt')) return { issue_prompt: 'Describe the bug' }
      return {}
    }))
    await openGeneratedPreview(user, 'github')

    expect(screen.getByRole('button', { name: 'Attach files' })).toBeDisabled()
    expect(screen.getByText('Checking attachment capabilities...')).toBeInTheDocument()
  })

  it('reads capabilities from ReportState without fetching /api/capabilities', async () => {
    const user = userEvent.setup()
    function WithLoadedReportCapabilities({ caps, children }: { caps: Capabilities | null; children: ReactNode }) {
      const dispatch = useReportDispatch()
      const { capabilitiesLoaded } = useReportState()
      useEffect(() => {
        dispatch({ type: 'SET_CAPABILITIES', payload: caps })
      }, [caps, dispatch])
      return capabilitiesLoaded ? <>{children}</> : null
    }
    render(
      <MemoryRouter>
        <ReportProvider>
          <WithLoadedReportCapabilities caps={makeAttachmentCapabilities()}>
            <BugCreationDialog open onOpenChange={() => {}} jobId="job-1" testName="test-a" target="jira" />
          </WithLoadedReportCapabilities>
        </ReportProvider>
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByLabelText('Issue Prompt')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /continue/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())

    expect(screen.getByRole('button', { name: 'Attach files' })).toBeEnabled()
    expect(screen.getByText('Up to 3 files (5 MiB each, 10 MiB total).')).toBeInTheDocument()
    expect(get.mock.calls.filter(([path]) => path === '/api/capabilities')).toHaveLength(0)
  })

  it('disables the picker with mapped reasons per target tracker and shows limits when available', async () => {
    const user = userEvent.setup()
    const caps = makeAttachmentCapabilities({
      report_github: { available: false, reason: 'public_base_url_required' },
      report_jira: { available: false, reason: 'tracker_disabled' },
    })

    await openPreviewWithCapabilities(user, 'github', caps)
    expect(screen.getByRole('button', { name: 'Attach files' })).toBeDisabled()
    expect(screen.getByText(/PUBLIC_BASE_URL/)).toBeInTheDocument()
    // Preloaded capabilities skip /api/capabilities network fetch
    expect(get.mock.calls.filter(([path]) => path === '/api/capabilities')).toHaveLength(0)
    cleanup()

    await openPreviewWithCapabilities(user, 'jira', caps)
    expect(screen.getByRole('button', { name: 'Attach files' })).toBeDisabled()
    expect(screen.getByText(/issue tracker is disabled/i)).toBeInTheDocument()
    cleanup()

    await openPreviewWithCapabilities(user, 'github', makeAttachmentCapabilities())
    expect(screen.getByRole('button', { name: 'Attach files' })).toBeEnabled()
    expect(screen.getByText('Up to 3 files (5 MiB each, 10 MiB total).')).toBeInTheDocument()
  })

  it('omits attachments when none are selected and encodes selected files inside guarded create', async () => {
    const user = userEvent.setup()
    const summary: IssueAttachmentSummary = {
      count: 1,
      types: ['text/plain'],
      native_attached: 0,
      linked: 1,
      failed_count: 0,
      files: [{ filename: 'keep.log', id: 'att-1', delivery: 'hosted', reason: 'hosted' }],
    }
    post.mockImplementation(async (path: string) => {
      if (path === previewPathFor('github')) return { title: 'Issue title', body: 'Issue body', similar_issues: [] }
      if (path === createPathFor('github')) {
        return { url: 'https://github.com/org/repo/issues/10', key: '', number: 10, title: 'Issue title', comment_id: 1, attachments: summary }
      }
      return {}
    })

    await openPreviewWithCapabilities(user, 'github', makeAttachmentCapabilities())
    const fileInput = screen.getByLabelText('Issue attachments')
    fireEvent.change(fileInput, {
      target: {
        files: [
          new File(['drop-me'], 'drop.log'),
          new File(['hello'], 'keep.log'),
        ],
      },
    })
    expect(screen.getByText('drop.log')).toBeInTheDocument()
    expect(screen.getByText('keep.log')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Remove drop.log' }))
    expect(screen.queryByText('drop.log')).not.toBeInTheDocument()

    // Double-click Create: submit guard prevents concurrent FileReader + POST execution
    const createBtn = screen.getByRole('button', { name: /create github issue/i })
    fireEvent.click(createBtn)
    fireEvent.click(createBtn)

    await waitFor(() => expect(screen.getByText('GitHub Issue created successfully.')).toBeInTheDocument())

    const previewCalls = post.mock.calls.filter(([path]) => path === previewPathFor('github'))
    expect(previewCalls[0][1]).not.toHaveProperty('attachments')

    const createCalls = post.mock.calls.filter(([path]) => path === createPathFor('github'))
    expect(createCalls).toHaveLength(1)
    expect(createCalls[0][1]).toMatchObject({
      title: 'Issue title',
      body: 'Issue body',
      attachments: [{ filename: 'keep.log', data_b64: btoa('hello') }],
    })

    // Hosted summary rendered without fallback/unknown warning
    const summaryBox = screen.getByTestId('attachment-delivery-summary')
    expect(summaryBox).toHaveTextContent('Attachments (1)')
    expect(summaryBox).toHaveTextContent('text/plain')
    expect(summaryBox).toHaveTextContent('keep.log')
    expect(summaryBox).toHaveTextContent('Hosted link in issue body')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('surfaces FileReader failure without POST or uncertain warning and retains files for retry', async () => {
    const user = userEvent.setup()
    post.mockImplementation(async (path: string) => {
      if (path === previewPathFor('github')) return { title: 'Issue title', body: 'Issue body', similar_issues: [] }
      if (path === createPathFor('github')) {
        return { url: 'https://github.com/org/repo/issues/11', key: '', number: 11, title: 'Issue title', comment_id: 1 }
      }
      return {}
    })
    await openPreviewWithCapabilities(user, 'github', makeAttachmentCapabilities())

    const fileInput = screen.getByLabelText('Issue attachments')
    fireEvent.change(fileInput, { target: { files: [new File(['bad'], 'unreadable.log')] } })

    const spy = vi.spyOn(FileReader.prototype, 'readAsDataURL').mockImplementationOnce(function (this: FileReader) {
      this.dispatchEvent(new ProgressEvent('error'))
    })

    await user.click(screen.getByRole('button', { name: /create github issue/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not read unreadable.log')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(post.mock.calls.filter(([path]) => path === createPathFor('github'))).toHaveLength(0)
    expect(screen.getByText('unreadable.log')).toBeInTheDocument()

    spy.mockRestore()
    await user.click(screen.getByRole('button', { name: /retry/i }))
    await waitFor(() => expect(screen.getByText('GitHub Issue created successfully.')).toBeInTheDocument())
    expect(post.mock.calls.filter(([path]) => path === createPathFor('github'))).toHaveLength(1)
  })

  it.each([
    [400, 'Bad Request', { detail: 'Unsupported attachment extension .svg on diagram.svg.' }, 'Unsupported attachment extension .svg'],
    [413, 'Payload Too Large', { detail: 'Request body exceeds 15 MiB attachment limit.' }, 'exceeds 15 MiB attachment limit'],
    [422, 'Unprocessable Entity', { detail: [{ msg: 'Invalid attachment payload schema' }] }, 'Invalid attachment payload schema'],
  ])('surfaces %i refusal visibly while retaining selected files for safe retry', async (status, statusText, body, expectedText) => {
    const user = userEvent.setup()
    mockCreateFlow('github', new ApiError(status, statusText, body))
    await openPreviewWithCapabilities(user, 'github', makeAttachmentCapabilities())

    fireEvent.change(screen.getByLabelText('Issue attachments'), {
      target: { files: [new File(['log-bytes'], 'trace.log')] },
    })

    await user.click(screen.getByRole('button', { name: /create github issue/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(expectedText)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByText('trace.log')).toBeInTheDocument()
    const retryBtn = screen.getByRole('button', { name: /retry/i })
    expect(retryBtn).toBeEnabled()

    await user.click(retryBtn)
    await waitFor(() => expect(screen.getByText('GitHub Issue created successfully.')).toBeInTheDocument())
  })

  it('warns never to resubmit on uncertain create while retaining files for inspection and blocking blind retry', async () => {
    const user = userEvent.setup()
    mockCreateFlow('github', new ApiError(502, 'Bad Gateway', { detail: 'GitHub did not answer.' }))
    await openPreviewWithCapabilities(user, 'github', makeAttachmentCapabilities())

    fireEvent.change(screen.getByLabelText('Issue attachments'), {
      target: { files: [new File(['evidence'], 'evidence.log')] },
    })

    await user.click(screen.getByRole('button', { name: /create github issue/i }))

    const warning = await screen.findByRole('status')
    expect(warning).toHaveTextContent(/may already have been filed.*never resubmit blindly/i)
    expect(warning).toHaveTextContent(/Selected files remain listed below for inspection/i)
    expect(screen.getByText('evidence.log')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /retry/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Remove evidence.log' })).toBeDisabled()
  })

  it('clears selected files when the dialog closes and reopens', async () => {
    const user = userEvent.setup()
    const caps = makeAttachmentCapabilities()
    const { rerender } = render(
      <MemoryRouter>
        <ReportProvider>
          <BugCreationDialog open onOpenChange={() => {}} jobId="job-1" testName="test-a" target="github" capabilities={caps} />
        </ReportProvider>
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByLabelText('Issue Prompt')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /continue/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())

    fireEvent.change(screen.getByLabelText('Issue attachments'), {
      target: { files: [new File(['abc'], 'session1.log')] },
    })
    expect(screen.getByText('session1.log')).toBeInTheDocument()

    rerender(
      <MemoryRouter>
        <ReportProvider>
          <BugCreationDialog open={false} onOpenChange={() => {}} jobId="job-1" testName="test-a" target="github" capabilities={caps} />
        </ReportProvider>
      </MemoryRouter>,
    )
    rerender(
      <MemoryRouter>
        <ReportProvider>
          <BugCreationDialog open onOpenChange={() => {}} jobId="job-1" testName="test-a" target="github" capabilities={caps} />
        </ReportProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    expect(screen.queryByText('session1.log')).not.toBeInTheDocument()
  })

  it('renders Jira fallback_only and native_outcome_unknown delivery summaries with duplicate-create warnings', async () => {
    const user = userEvent.setup()
    const jiraSummary: IssueAttachmentSummary = {
      count: 3,
      types: ['image/png', 'text/plain'],
      native_attached: 1,
      linked: 3,
      failed_count: 2,
      files: [
        { filename: 'ok.png', id: 'id-1', delivery: 'native_and_hosted', reason: 'native_attached' },
        { filename: 'denied.log', id: 'id-2', delivery: 'fallback_only', reason: 'native_refused' },
        { filename: 'slow.log', id: 'id-3', delivery: 'native_outcome_unknown', reason: 'native_outcome_unknown' },
      ],
    }
    post.mockImplementation(async (path: string) => {
      if (path === previewPathFor('jira')) return { title: 'Jira bug', body: 'Bug details', similar_issues: [] }
      if (path === createPathFor('jira')) {
        return { url: 'https://jira.example.com/browse/MTV-9', key: 'MTV-9', title: 'Jira bug', comment_id: 2, attachments: jiraSummary }
      }
      return {}
    })

    await openPreviewWithCapabilities(user, 'jira', makeAttachmentCapabilities())
    fireEvent.change(screen.getByLabelText('Issue attachments'), {
      target: { files: [new File(['1'], 'ok.png'), new File(['2'], 'denied.log'), new File(['3'], 'slow.log')] },
    })

    await user.click(screen.getByRole('button', { name: /create jira ticket/i }))
    await waitFor(() => expect(screen.getByText('Jira Ticket created successfully.')).toBeInTheDocument())

    const summaryBox = screen.getByTestId('attachment-delivery-summary')
    expect(summaryBox).toHaveTextContent('Attachments (3)')
    expect(summaryBox).toHaveTextContent('ok.png')
    expect(summaryBox).toHaveTextContent('Attached in Jira + hosted link')
    expect(summaryBox).toHaveTextContent('denied.log')
    expect(summaryBox).toHaveTextContent('Hosted fallback link only (Jira refused native upload)')
    expect(summaryBox).toHaveTextContent('slow.log')
    expect(summaryBox).toHaveTextContent('Jira native outcome unknown; hosted link in issue body')

    const statusBox = screen.getByRole('status')
    expect(statusBox).toHaveTextContent(/One or more Jira native uploads were unavailable.*Do not resubmit/i)
    expect(statusBox).toHaveTextContent(/Jira native upload outcome is unknown.*Never resubmit/i)
  })
})
