import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { ReportProvider } from '../ReportContext'
import { BugCreationDialog } from '../BugCreationDialog'
import { resetProviderCatalogCache } from '@/lib/useProviderOptions'
import { ApiError } from '@/lib/api'
import { SERVER_ACCESS_HELP } from '@/components/shared/CredentialSourceLabel'

HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

const get = vi.fn()
const post = vi.fn()
let grant = true

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
      const detail = err instanceof _ApiError ? (err.body as { detail?: unknown })?.detail : null
      return typeof detail === 'string' ? detail : null
    },
  }
})
vi.mock('@/lib/cookies', () => ({
  getGithubToken: () => 'ghp_test_token',
  getJiraToken: () => '',
  getJiraEmail: () => '',
  getJiraSaToken: () => 'jira-sa-token',
}))
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
  get.mockImplementation(async (path: string) => {
    if (path.startsWith('/api/ai-models')) return catalog
    if (path.endsWith('/issue-prompt')) return { issue_prompt: 'Describe the bug' }
    return {}
  })
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

  it('adopts an issue filed while the create was failing instead of duplicating it', async () => {
    const user = userEvent.setup()
    mockCreateFlow('github', new ApiError(502, 'Bad Gateway', { detail: 'GitHub is unavailable right now.' }))
    // The POST reports a failure, but the server did file the issue: its
    // tracked-in link appears between the snapshot and the failure.
    let reads = 0
    get.mockImplementation(async (path: string) => {
      if (path.startsWith('/api/ai-models')) return catalog
      if (path.endsWith('/issue-prompt')) return { issue_prompt: 'Describe the bug' }
      if (path.endsWith('/tracked-in')) {
        reads += 1
        return reads === 1 ? { tracked_in: {} } : { tracked_in: { 'test-a': [{ id: 7, tracked_in_url: 'https://tracker/issue/7', tracked_in_type: 'github', tracked_in_by: 'alice' }] } }
      }
      return {}
    })
    await openGeneratedPreview(user)

    await user.click(screen.getByRole('button', { name: /create github issue/i }))

    expect(await screen.findByText('GitHub Issue created successfully.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /open github issue/i })).toHaveAttribute('href', 'https://tracker/issue/7')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    // No duplicate POST: one create attempt, and the failure was not retried.
    expect(post.mock.calls.filter(([path]) => path === createPathFor('github'))).toHaveLength(1)
  })

  it('never reports another failure\'s new link as this create succeeding', async () => {
    const user = userEvent.setup()
    mockCreateFlow('github', new ApiError(502, 'Bad Gateway', { detail: 'GitHub is unavailable right now.' }))
    // Someone else files an issue for a different test while our POST fails.
    get.mockImplementation(async (path: string) => {
      if (path.startsWith('/api/ai-models')) return catalog
      if (path.endsWith('/issue-prompt')) return { issue_prompt: 'Describe the bug' }
      if (path.endsWith('/tracked-in')) {
        return { tracked_in: { 'test-b': [{ id: 9, tracked_in_url: 'https://tracker/issue/9', tracked_in_type: 'github', tracked_in_by: 'bob' }] } }
      }
      return {}
    })
    await openGeneratedPreview(user)

    await user.click(screen.getByRole('button', { name: /create github issue/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent('GitHub is unavailable right now.')
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
    expect(screen.queryByText('GitHub Issue created successfully.')).not.toBeInTheDocument()
  })

  it('still offers the retry when the tracked-in check itself fails', async () => {
    const user = userEvent.setup()
    mockCreateFlow('github', new ApiError(502, 'Bad Gateway', { detail: 'GitHub is unavailable right now.' }))
    get.mockImplementation(async (path: string) => {
      if (path.startsWith('/api/ai-models')) return catalog
      if (path.endsWith('/issue-prompt')) return { issue_prompt: 'Describe the bug' }
      if (path.endsWith('/tracked-in')) throw new Error('tracked-in unavailable')
      return {}
    })
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
