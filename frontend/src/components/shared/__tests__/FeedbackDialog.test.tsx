import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { FeedbackDialog } from '../FeedbackDialog'
import type { Capabilities, IssueAttachmentSummary } from '@/types'

// The account row is the dialog's only credential source.
const mockUserTokens = vi.fn(() => ({
  github_token: 'ghp_test_token',
  jira_email: '',
  jira_token: '',
  jira_sa_token: '',
}))

const defaultGet = (path?: string) =>
  Promise.resolve(
    path === '/api/user/tokens'
      ? mockUserTokens()
      : { providers: {}, provider_status: {} },
  )

// Mock the api module
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
    api: {
      post: vi.fn(),
      get: vi.fn((path?: string) => defaultGet(path)),
    },
    getRecentFailedCalls: vi.fn(() => []),
    ApiError: _ApiError,
    extractApiDetail: (err: unknown): string | null => {
      if (err instanceof _ApiError) {
        const detail = (err.body as { detail?: unknown })?.detail
        if (typeof detail === 'string' && detail) return detail
        if (Array.isArray(detail)) {
          const msgs = detail
            .map((item) => (typeof item?.msg === 'string' ? item.msg.replace(/^Value error, /, '') : ''))
            .filter(Boolean)
          return msgs.length > 0 ? msgs.join('; ') : null
        }
      }
      return null
    },
  }
})

// Mock errorCapture
vi.mock('@/lib/errorCapture', () => ({
  getRecentErrors: vi.fn(() => ['error1', 'error2']),
}))

// The dialog is rendered bare, so it has no AuthProvider; the hook reads the user key.
vi.mock('@/lib/auth', () => ({
  useAuth: () => ({
    username: 'alice', isAdmin: false, authenticated: true, loading: false,
    canUseServerProviders: false, canUseFreeProviders: false,
  }),
}))

// The picker needs auth context; its own behaviour is covered in BugCreationDialog tests.
// The value is exposed so the remembered preselection is assertable.
vi.mock('@/components/shared/AnalysisAiPicker', () => ({
  AnalysisProviderSelect: ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <>
      <span data-testid="provider-value">{value}</span>
      <button type="button" aria-label="pick-provider" onClick={() => onChange('claude')} />
    </>
  ),
  AnalysisModelSelect: ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <>
      <span data-testid="model-value">{value}</span>
      <button type="button" aria-label="pick-model" onClick={() => onChange('sonnet')} />
    </>
  ),
}))

import { api, getRecentFailedCalls } from '@/lib/api'

const mockPost = api.post as ReturnType<typeof vi.fn>
const mockGet = api.get as ReturnType<typeof vi.fn>
const mockGetFailedCalls = getRecentFailedCalls as ReturnType<typeof vi.fn>

/** Wrapper that owns the open state so the dialog can be closed and reopened. */
function ReopenableFeedbackDialog() {
  const [open, setOpen] = useState(true)
  return (
    <>
      <button type="button" data-testid="reopen" onClick={() => setOpen(true)}>reopen</button>
      <FeedbackDialog open={open} onOpenChange={setOpen} />
    </>
  )
}

describe('FeedbackDialog', () => {
  const onOpenChange = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    mockGet.mockImplementation((path?: string) => defaultGet(path))
  })

  afterEach(() => {
    localStorage.clear()
  })

  it('renders form with description textarea when open', () => {
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    expect(screen.getByText('Send Feedback')).toBeInTheDocument()
    expect(screen.getByText(/Describe your issue or idea/)).toBeInTheDocument()
    expect(screen.getByLabelText('Description')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('Describe your issue or suggestion...')).toBeInTheDocument()
  })

  it('omits ai_provider/ai_model so the server default is resolved', async () => {
    mockPost.mockResolvedValue({ title: 'T', body: 'B', labels: ['bug'] })
    mockGetFailedCalls.mockReturnValue([])
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'No pair chosen')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    const payload = mockPost.mock.calls[0][1] as Record<string, unknown>
    expect('ai_provider' in payload).toBe(false)
    expect('ai_model' in payload).toBe(false)
  })

  it('shows the server-resolved provider/model in the preview', async () => {
    mockPost.mockResolvedValue({
      title: 'T',
      body: 'B',
      labels: ['bug'],
      ai_provider: 'claude',
      ai_model: 'sonnet',
      ai_generated: true,
    })
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'x')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    expect(await screen.findByText('Generated using AI with claude/sonnet')).toBeInTheDocument()
  })

  it('says no AI model generated the issue when feedback fell back', async () => {
    mockPost.mockResolvedValue({
      title: 'T',
      body: 'B',
      labels: ['bug'],
      ai_provider: 'claude',
      ai_model: 'sonnet',
      ai_generated: false,
    })
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'x')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    expect(await screen.findByText(/No AI model generated this issue/)).toBeInTheDocument()
  })

  it('disables Preview when description is empty', () => {
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    expect(screen.getByRole('button', { name: /preview/i })).toBeDisabled()
  })

  it('enables Preview when description is provided', async () => {
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Something broke')
    expect(screen.getByRole('button', { name: /preview/i })).toBeEnabled()
  })

  it('calls preview endpoint and shows editable preview', async () => {
    mockPost.mockResolvedValue({
      title: 'AI generated title',
      body: 'AI generated body',
      labels: ['bug'],
    })
    mockGetFailedCalls.mockReturnValue([])
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Page crashes on load')
    await user.click(screen.getByRole('button', { name: /preview/i }))

    await waitFor(() => expect(screen.getByText('Preview Issue')).toBeInTheDocument())
    expect(screen.getByLabelText('Title')).toHaveValue('AI generated title')
    expect(screen.getByLabelText('Body')).toHaveValue('AI generated body')
    expect(mockPost).toHaveBeenCalledWith('/api/feedback/preview', expect.objectContaining({
      description: 'Page crashes on load',
      user_agent: expect.any(String),
      console_errors: ['error1', 'error2'],
    }))
  })

  it('allows editing preview title and body', async () => {
    mockPost.mockResolvedValue({
      title: 'Original title',
      body: 'Original body',
      labels: ['bug'],
    })
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Some bug')
    await user.click(screen.getByRole('button', { name: /preview/i }))

    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())

    const titleInput = screen.getByLabelText('Title')
    await user.clear(titleInput)
    await user.type(titleInput, 'Edited title')
    expect(titleInput).toHaveValue('Edited title')

    const bodyInput = screen.getByLabelText('Body')
    await user.clear(bodyInput)
    await user.type(bodyInput, 'Edited body')
    expect(bodyInput).toHaveValue('Edited body')
  })

  it('creates issue from preview and shows success', async () => {
    // First call: preview
    mockPost.mockResolvedValueOnce({
      title: 'AI title',
      body: 'AI body',
      labels: ['bug'],
    })
    // Second call: create
    mockPost.mockResolvedValueOnce({
      issue_url: 'https://github.com/org/repo/issues/42',
      issue_number: 42,
      title: 'AI title',
    })

    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Page crashes')
    await user.click(screen.getByRole('button', { name: /preview/i }))

    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /create issue/i }))

    await waitFor(() => expect(screen.getByText('Thank you for your feedback!')).toBeInTheDocument())
    expect(screen.getByText('View issue')).toHaveAttribute('href', 'https://github.com/org/repo/issues/42')
    expect(mockPost).toHaveBeenCalledWith('/api/feedback/create', {
      title: 'AI title',
      body: 'AI body',
      labels: ['bug'],
    })
  })

  it('Back button returns to form from preview', async () => {
    mockPost.mockResolvedValue({
      title: 'AI title',
      body: 'AI body',
      labels: ['bug'],
    })
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Some feedback')
    await user.click(screen.getByRole('button', { name: /preview/i }))

    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /back/i }))

    expect(screen.getByText('Send Feedback')).toBeInTheDocument()
    expect(screen.getByLabelText('Description')).toBeInTheDocument()
  })

  it('shows error message on preview failure', async () => {
    mockPost.mockRejectedValue(new Error('API error 500: Internal Server Error'))
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Some feedback')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByText(/API error 500/)).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })

  it('shows error message on create failure and keeps the preview retryable', async () => {
    mockPost.mockResolvedValueOnce({
      title: 'AI title',
      body: 'AI body',
      labels: ['bug'],
    })
    mockPost.mockRejectedValueOnce(new Error('API error 500: Internal Server Error'))

    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Some feedback')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /create issue/i }))

    await waitFor(() => expect(screen.getByText(/API error 500/)).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
  })

  it('Try Again from preview error returns to form', async () => {
    mockPost.mockRejectedValue(new Error('Network error'))
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Some feedback')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByText(/Network error/)).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /try again/i }))
    expect(screen.getByText('Send Feedback')).toBeInTheDocument()
    expect(screen.getByLabelText('Description')).toBeInTheDocument()
  })

  it('keeps the generated issue editable after a failed create', async () => {
    mockPost.mockResolvedValueOnce({
      title: 'AI title',
      body: 'AI body',
      labels: ['bug'],
    })
    const { ApiError } = await import('@/lib/api')
    mockPost.mockRejectedValueOnce(new ApiError(502, 'Bad Gateway', {
      detail: 'GitHub is unavailable right now, so your feedback was not filed. Try again in a few minutes.',
    }))

    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Some feedback')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /create issue/i }))

    // Server detail shown verbatim, dialog still open, generated text still editable.
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'GitHub is unavailable right now, so your feedback was not filed. Try again in a few minutes.',
    )
    expect(screen.getByText('Preview Issue')).toBeInTheDocument()
    expect(screen.getByLabelText('Title')).toHaveValue('AI title')
    expect(screen.getByLabelText('Body')).toHaveValue('AI body')

    const titleInput = screen.getByLabelText('Title')
    await user.clear(titleInput)
    await user.type(titleInput, 'Edited after failure')
    expect(titleInput).toHaveValue('Edited after failure')
  })

  it('Retry resubmits the same payload without regenerating', async () => {
    mockPost.mockResolvedValueOnce({
      title: 'AI title',
      body: 'AI body',
      labels: ['bug'],
    })
    mockPost.mockRejectedValueOnce(new Error('Create failed'))
    mockPost.mockResolvedValueOnce({
      issue_url: 'https://github.com/org/repo/issues/7',
      issue_number: 7,
      title: 'AI title',
    })

    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Some feedback')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /create issue/i }))
    await waitFor(() => expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /retry/i }))
    await waitFor(() => expect(screen.getByText('Thank you for your feedback!')).toBeInTheDocument())

    const callsByPath = (path: string) => mockPost.mock.calls.filter(([p]) => p === path)
    expect(callsByPath('/api/feedback/preview')).toHaveLength(1)
    const createCalls = callsByPath('/api/feedback/create')
    expect(createCalls).toHaveLength(2)
    expect(createCalls[1][1]).toEqual({ title: 'AI title', body: 'AI body', labels: ['bug'] })
  })

  it('clears a stale create error when a new preview is generated', async () => {
    mockPost.mockResolvedValueOnce({ title: 'AI title', body: 'AI body', labels: ['bug'] })
    mockPost.mockRejectedValueOnce(new Error('Create failed'))
    mockPost.mockResolvedValueOnce({ title: 'Second title', body: 'Second body', labels: ['bug'] })

    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Some feedback')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /create issue/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Create failed')

    // Back to the form and generate a fresh preview: the old failure belongs to
    // the previous issue, so it must not paint the new one.
    await user.click(screen.getByRole('button', { name: /back/i }))
    await user.click(screen.getByRole('button', { name: /preview/i }))

    await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('Second title'))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument()
  })

  it('falls back to the generic message when the server sends no detail', async () => {
    mockPost.mockResolvedValueOnce({ title: 'AI title', body: 'AI body', labels: ['bug'] })
    const { ApiError } = await import('@/lib/api')
    mockPost.mockRejectedValueOnce(new ApiError(502, 'Bad Gateway', {}))

    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Some feedback')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /create issue/i }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Failed to create issue')
    // The HTTP status text is never shown to the user.
    expect(alert).not.toHaveTextContent('Bad Gateway')
  })

  it('double-clicking Retry submits once', async () => {
    mockPost.mockResolvedValueOnce({ title: 'AI title', body: 'AI body', labels: ['bug'] })
    mockPost.mockRejectedValueOnce(new Error('Create failed'))
    mockPost.mockResolvedValueOnce({
      issue_url: 'https://github.com/org/repo/issues/8',
      issue_number: 8,
      title: 'AI title',
    })

    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Some feedback')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /create issue/i }))
    await waitFor(() => expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument())

    const retryButton = screen.getByRole('button', { name: /retry/i })
    fireEvent.click(retryButton)
    fireEvent.click(retryButton)
    await waitFor(() => expect(screen.getByText('Thank you for your feedback!')).toBeInTheDocument())

    // One failed create + exactly one retry.
    expect(mockPost.mock.calls.filter(([p]) => p === '/api/feedback/create')).toHaveLength(2)
  })

  it('includes failed API calls in the preview payload', async () => {
    mockPost.mockResolvedValue({ title: 'T', body: 'B', labels: [] })
    mockGetFailedCalls.mockReturnValue([
      { status: 500, endpoint: '/api/test', error: 'server error', timestamp: 123 },
    ])
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Bug report')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(mockPost).toHaveBeenCalled())
    const payload = mockPost.mock.calls[0][1]
    expect(payload.failed_api_calls).toEqual([
      { status: 500, endpoint: '/api/test', error: 'server error' },
    ])
  })

  it('sends edited title and body when creating issue', async () => {
    mockPost.mockResolvedValueOnce({
      title: 'Original title',
      body: 'Original body',
      labels: ['enhancement'],
    })
    mockPost.mockResolvedValueOnce({
      issue_url: 'https://github.com/org/repo/issues/99',
      issue_number: 99,
      title: 'Edited title',
    })

    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'A feature idea')
    await user.click(screen.getByRole('button', { name: /preview/i }))

    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())

    const titleInput = screen.getByLabelText('Title')
    await user.clear(titleInput)
    await user.type(titleInput, 'Edited title')

    const bodyInput = screen.getByLabelText('Body')
    await user.clear(bodyInput)
    await user.type(bodyInput, 'Edited body')

    await user.click(screen.getByRole('button', { name: /create issue/i }))

    await waitFor(() => expect(screen.getByText('Thank you for your feedback!')).toBeInTheDocument())
    expect(mockPost).toHaveBeenCalledWith('/api/feedback/create', {
      title: 'Edited title',
      body: 'Edited body',
      labels: ['enhancement'],
    })
  })

  it('shows AI-not-configured state with manual issue link when AI is not configured', async () => {
    mockPost.mockRejectedValue(new Error('AI is not configured'))
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Some feedback')
    await user.click(screen.getByRole('button', { name: /preview/i }))

    await waitFor(() =>
      expect(screen.getByText(/AI is not configured on this server/)).toBeInTheDocument()
    )
    const link = screen.getByRole('link', { name: /open a new issue/i })
    expect(link).toHaveAttribute('href', 'https://github.com/myk-org/rootcoz/issues/new')
    expect(link).toHaveAttribute('target', '_blank')
    // Should show Close button in footer, not Try Again
    const closeButtons = screen.getAllByRole('button', { name: /close/i })
    expect(closeButtons.length).toBeGreaterThanOrEqual(1)
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()
  })

  it('handles "ai not configured" error with varied casing', async () => {
    mockPost.mockRejectedValue(new Error('AI Not Configured on this instance'))
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Feedback')
    await user.click(screen.getByRole('button', { name: /preview/i }))

    await waitFor(() =>
      expect(screen.getByText(/AI is not configured on this server/)).toBeInTheDocument()
    )
  })

  it('shows no token warning while the account read is still pending', async () => {
    mockGet.mockImplementation((path?: string) =>
      path === '/api/user/tokens'
        ? new Promise(() => {})
        : Promise.resolve({ providers: {}, provider_status: {} }),
    )
    mockPost.mockResolvedValue({ title: 'AI title', body: 'AI body', labels: ['bug'] })
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Some feedback')
    await user.click(screen.getByRole('button', { name: /preview/i }))

    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    // Unknown is not empty: the read has not answered, so there is nothing to warn
    // about and nothing to block on.
    expect(screen.queryByText('GitHub token required')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /create issue/i })).toBeEnabled()
  })

  it('shows warning banner and disables Create Issue when no GitHub token', async () => {
    mockUserTokens.mockReturnValue({ github_token: '', jira_email: '', jira_token: '', jira_sa_token: '' })
    mockPost.mockResolvedValue({
      title: 'AI title',
      body: 'AI body',
      labels: ['bug'],
    })
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Some feedback')
    await user.click(screen.getByRole('button', { name: /preview/i }))

    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    expect(screen.getByText('GitHub token required')).toBeInTheDocument()
    expect(screen.getByText(/Set up your GitHub token in Profile Settings/)).toBeInTheDocument()
    expect(screen.getByText('Open Profile Settings →')).toHaveAttribute('href', '/settings')
    expect(screen.getByRole('button', { name: /create issue/i })).toBeDisabled()
    mockUserTokens.mockReturnValue({
      github_token: 'ghp_test_token',
      jira_email: '',
      jira_token: '',
      jira_sa_token: '',
    })
  })

  it('shows token hint in error when create fails with token error', async () => {
    mockPost.mockResolvedValueOnce({
      title: 'AI title',
      body: 'AI body',
      labels: ['bug'],
    })
    const { ApiError } = await import('@/lib/api')
    mockPost.mockRejectedValueOnce(new ApiError(400, 'Bad Request', { detail: 'GitHub token is required. Set up your token in Profile Settings.' }))

    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Some feedback')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /create issue/i }))

    await waitFor(() => expect(screen.getByText(/GitHub token is required/)).toBeInTheDocument())
    expect(screen.getByText(/You can update your tokens/)).toBeInTheDocument()
  })

  it('sends the pair once both a provider and a model are chosen', async () => {
    mockPost.mockResolvedValue({ title: 'T', body: 'B', labels: [] })
    mockGetFailedCalls.mockReturnValue([])
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.click(screen.getByLabelText('pick-provider'))
    await user.click(screen.getByLabelText('pick-model'))
    await user.type(screen.getByLabelText('Description'), 'x')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(mockPost).toHaveBeenCalled())
    const payload = mockPost.mock.calls[0][1] as Record<string, unknown>
    expect(payload.ai_provider).toBe('claude')
    expect(payload.ai_model).toBe('sonnet')
  })

  it('omits the pair entirely when a provider is chosen without a model', async () => {
    mockPost.mockResolvedValue({ title: 'T', body: 'B', labels: [] })
    mockGetFailedCalls.mockReturnValue([])
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.click(screen.getByLabelText('pick-provider'))
    await user.type(screen.getByLabelText('Description'), 'x')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(mockPost).toHaveBeenCalled())
    const payload = mockPost.mock.calls[0][1] as Record<string, unknown>
    expect('ai_provider' in payload).toBe(false)
    expect('ai_model' in payload).toBe(false)
  })

  it('remembers the pair after close and reopen', async () => {
    mockPost.mockResolvedValue({ title: 'T', body: 'B', labels: [] })
    mockGetFailedCalls.mockReturnValue([])
    const user = userEvent.setup()
    render(<ReopenableFeedbackDialog />)

    await user.click(screen.getByLabelText('pick-provider'))
    await user.click(screen.getByLabelText('pick-model'))
    await user.type(screen.getByLabelText('Description'), 'first session')
    await user.click(screen.getByRole('button', { name: /cancel/i }))

    // handleClose resets on a 200ms delay (close animation); reopening cancels that timer.
    await act(async () => { await new Promise((r) => setTimeout(r, 300)) })
    fireEvent.click(screen.getByTestId('reopen'))

    // The description is per dialog session and still resets; only the pair is remembered.
    expect(screen.getByLabelText('Description')).toHaveValue('')
    await user.type(screen.getByLabelText('Description'), 'second session')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(mockPost).toHaveBeenCalled())
    const payload = mockPost.mock.calls[0][1] as Record<string, unknown>
    expect(payload.ai_provider).toBe('claude')
    expect(payload.ai_model).toBe('sonnet')
  })

  it('preselects the pair remembered for this user and sends it', async () => {
    localStorage.setItem('rootcoz_last_ai_pair:alice', JSON.stringify({ provider: 'claude', model: 'sonnet' }))
    mockPost.mockResolvedValue({ title: 'T', body: 'B', labels: [] })
    mockGetFailedCalls.mockReturnValue([])
    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)

    expect(screen.getByTestId('provider-value')).toHaveTextContent('claude')
    expect(screen.getByTestId('model-value')).toHaveTextContent('sonnet')

    await user.type(screen.getByLabelText('Description'), 'x')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(mockPost).toHaveBeenCalled())
    const payload = mockPost.mock.calls[0][1] as Record<string, unknown>
    expect(payload.ai_provider).toBe('claude')
    expect(payload.ai_model).toBe('sonnet')
  })
})

function makeFeedbackCapabilities(
  feedbackOverride?: Capabilities['issue_attachments']['feedback'],
  attachmentOverrides?: Partial<Capabilities['issue_attachments']>,
): Capabilities {
  return {
    github_issues_enabled: true,
    jira_issues_enabled: true,
    issue_attachments: {
      max_files: 3,
      max_file_bytes: 5242880,
      max_total_bytes: 10485760,
      max_run_bytes: 209715200,
      max_owner_bytes: 524288000,
      types: ['text/plain', 'image/png', 'image/jpeg', 'image/gif', 'image/webp'],
      report_github: { available: true, reason: 'available' },
      report_jira: { available: true, reason: 'available' },
      feedback: feedbackOverride ?? { available: true, reason: 'available' },
      chat_ui: { available: false, reason: 'browser_chat_unsupported' },
      direct_chat: { available: true, reason: 'available' },
      ...attachmentOverrides,
    },
  }
}

describe('FeedbackDialog issue attachments', () => {
  const onOpenChange = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    mockGet.mockImplementation((path?: string) => {
      if (path === '/api/capabilities') return Promise.resolve(makeFeedbackCapabilities())
      return defaultGet(path)
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
  })

  it('fetches /api/capabilities on open and disables picker when pending, failed, or unavailable', async () => {
    const user = userEvent.setup()
    mockPost.mockResolvedValue({ title: 'Preview T', body: 'Preview B', labels: ['bug'] })

    // Pending capability fetch disables picker
    mockGet.mockImplementation((path?: string) => {
      if (path === '/api/capabilities') return new Promise(() => {})
      return defaultGet(path)
    })
    const { unmount } = render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Desc')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Attach files' })).toBeDisabled()
    expect(screen.getByText('Checking attachment capabilities...')).toBeInTheDocument()
    unmount()

    // PUBLIC_BASE_URL missing disables picker with explicit setting name
    mockGet.mockImplementation((path?: string) => {
      if (path === '/api/capabilities') {
        return Promise.resolve(makeFeedbackCapabilities({ available: false, reason: 'public_base_url_required' }))
      }
      return defaultGet(path)
    })
    const second = render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Desc')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Attach files' })).toBeDisabled()
    expect(screen.getByText(/PUBLIC_BASE_URL/)).toBeInTheDocument()
    second.unmount()

    // Tracker disabled reason
    mockGet.mockImplementation((path?: string) => {
      if (path === '/api/capabilities') {
        return Promise.resolve(makeFeedbackCapabilities({ available: false, reason: 'tracker_disabled' }))
      }
      return defaultGet(path)
    })
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Desc')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Attach files' })).toBeDisabled()
    expect(screen.getByText(/issue tracker is disabled/i)).toBeInTheDocument()
  })

  it('sends no attachments on preview, encodes inside guarded create, and renders delivery summary on success', async () => {
    const user = userEvent.setup()
    const summary: IssueAttachmentSummary = {
      count: 1,
      types: ['text/plain'],
      native_attached: 0,
      linked: 1,
      failed_count: 0,
      files: [{ filename: 'console.log', id: 'fb-att-1', delivery: 'hosted', reason: 'hosted' }],
    }
    mockPost.mockImplementation(async (path: string) => {
      if (path === '/api/feedback/preview') return { title: 'Preview T', body: 'Preview B', labels: ['bug'] }
      if (path === '/api/feedback/create') {
        return { issue_url: 'https://github.com/org/repo/issues/55', issue_number: 55, title: 'Preview T', attachments: summary }
      }
      return {}
    })

    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'UI issue')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())

    expect(screen.getByRole('button', { name: 'Attach files' })).toBeEnabled()
    expect(screen.getByText('Up to 3 files (5 MiB each, 10 MiB total).')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Issue attachments'), {
      target: { files: [new File(['remove'], 'skip.txt'), new File(['stacktrace'], 'console.log')] },
    })
    await user.click(screen.getByRole('button', { name: 'Remove skip.txt' }))

    const createBtn = screen.getByRole('button', { name: /create issue/i })
    fireEvent.click(createBtn)
    fireEvent.click(createBtn)

    await waitFor(() => expect(screen.getByText('Thank you for your feedback!')).toBeInTheDocument())

    const previewPayload = mockPost.mock.calls.find(([p]) => p === '/api/feedback/preview')?.[1] as Record<string, unknown>
    expect(previewPayload).not.toHaveProperty('attachments')

    const createCalls = mockPost.mock.calls.filter(([p]) => p === '/api/feedback/create')
    expect(createCalls).toHaveLength(1)
    expect(createCalls[0][1]).toEqual({
      title: 'Preview T',
      body: 'Preview B',
      labels: ['bug'],
      attachments: [{ filename: 'console.log', data_b64: btoa('stacktrace') }],
    })

    const summaryBox = screen.getByTestId('attachment-delivery-summary')
    expect(summaryBox).toHaveTextContent('Attachments (1)')
    expect(summaryBox).toHaveTextContent('console.log')
    expect(summaryBox).toHaveTextContent('Hosted link in issue body')
  })

  it('retains selected files on FileReader error and 400/413/422 refusals for safe retry', async () => {
    const user = userEvent.setup()
    const { ApiError } = await import('@/lib/api')
    mockPost
      .mockResolvedValueOnce({ title: 'Preview T', body: 'Preview B', labels: ['bug'] })
      .mockRejectedValueOnce(new ApiError(400, 'Bad Request', { detail: 'SVG attachments are not permitted.' }))
      .mockRejectedValueOnce(new ApiError(413, 'Payload Too Large', { detail: 'Request body exceeds 15 MiB.' }))
      .mockRejectedValueOnce(new ApiError(422, 'Unprocessable Entity', { detail: [{ msg: 'Malformed attachment item' }] }))
      .mockResolvedValueOnce({ issue_url: 'https://github.com/org/repo/issues/60', issue_number: 60, title: 'Preview T' })

    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Feedback with file')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())

    fireEvent.change(screen.getByLabelText('Issue attachments'), {
      target: { files: [new File(['data'], 'report.log')] },
    })

    // 1) FileReader error: no POST sent, no uncertain warning, file retained
    const readSpy = vi.spyOn(FileReader.prototype, 'readAsDataURL').mockImplementationOnce(function (this: FileReader) {
      this.dispatchEvent(new ProgressEvent('error'))
    })
    await user.click(screen.getByRole('button', { name: /create issue/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not read report.log')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(mockPost.mock.calls.filter(([p]) => p === '/api/feedback/create')).toHaveLength(0)
    expect(screen.getByText('report.log')).toBeInTheDocument()
    readSpy.mockRestore()

    // 2) 400 refusal: file retained, Retry enabled
    await user.click(screen.getByRole('button', { name: /retry/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('SVG attachments are not permitted.')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByText('report.log')).toBeInTheDocument()

    // 3) 413 refusal: file retained, Retry enabled
    await user.click(screen.getByRole('button', { name: /retry/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Request body exceeds 15 MiB.')
    expect(screen.getByText('report.log')).toBeInTheDocument()

    // 4) 422 refusal: file retained, Retry enabled
    await user.click(screen.getByRole('button', { name: /retry/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Malformed attachment item')
    expect(screen.getByText('report.log')).toBeInTheDocument()

    // 5) Retry succeeds
    await user.click(screen.getByRole('button', { name: /retry/i }))
    await waitFor(() => expect(screen.getByText('Thank you for your feedback!')).toBeInTheDocument())
  })

  it('warns against blind resubmit on uncertain create while retaining files, and clears stale warning on new preview', async () => {
    const user = userEvent.setup()
    const { ApiError } = await import('@/lib/api')
    mockPost
      .mockResolvedValueOnce({ title: 'First T', body: 'First B', labels: ['bug'] })
      .mockRejectedValueOnce(new ApiError(502, 'Bad Gateway', { detail: 'Upstream gateway timed out.' }))
      .mockResolvedValueOnce({ title: 'Fresh T', body: 'Fresh B', labels: ['bug'] })

    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Ambiguous feedback')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())

    fireEvent.change(screen.getByLabelText('Issue attachments'), {
      target: { files: [new File(['important'], 'evidence.log')] },
    })

    await user.click(screen.getByRole('button', { name: /create issue/i }))

    const warning = await screen.findByRole('status')
    expect(warning).toHaveTextContent(/may already have been filed.*never resubmit blindly/i)
    expect(warning).toHaveTextContent(/Selected files remain listed below for inspection/i)
    expect(screen.getByText('evidence.log')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /retry/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Remove evidence.log' })).toBeDisabled()

    // Going Back and generating a fresh preview clears both createError and createMaybeFiled
    await user.click(screen.getByRole('button', { name: /back/i }))
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toHaveValue('Fresh T'))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /create issue/i })).toBeEnabled()
  })

  it('clears selected files on close and reopen', async () => {
    const user = userEvent.setup()
    mockPost.mockResolvedValue({ title: 'T', body: 'B', labels: ['bug'] })
    render(<ReopenableFeedbackDialog />)

    await user.type(screen.getByLabelText('Description'), 'First session')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())

    fireEvent.change(screen.getByLabelText('Issue attachments'), {
      target: { files: [new File(['bytes'], 'temp.log')] },
    })
    expect(screen.getByText('temp.log')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /cancel/i }))
    await act(async () => { await new Promise((r) => setTimeout(r, 250)) })
    fireEvent.click(screen.getByTestId('reopen'))

    await user.type(screen.getByLabelText('Description'), 'Second session')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    expect(screen.queryByText('temp.log')).not.toBeInTheDocument()
  })

  it('enforces capability count, per-file, and cumulative limits in the picker and before create encoding', async () => {
    const user = userEvent.setup()
    const readSpy = vi.spyOn(FileReader.prototype, 'readAsDataURL')
    mockPost.mockResolvedValue({ title: 'Preview T', body: 'Preview B', labels: ['bug'] })

    const initialCaps = makeFeedbackCapabilities(undefined, {
      max_files: 2,
      max_file_bytes: 10,
      max_total_bytes: 15,
    })
    const { rerender } = render(
      <FeedbackDialog open={true} onOpenChange={onOpenChange} capabilities={initialCaps} />,
    )
    await user.type(screen.getByLabelText('Description'), 'Oversized feedback selection')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())

    const fileInput = screen.getByLabelText('Issue attachments')

    // Count rejection (> 2 files)
    fireEvent.change(fileInput, {
      target: { files: [new File(['a'], '1.log'), new File(['b'], '2.log'), new File(['c'], '3.log')] },
    })
    expect(screen.getByRole('alert')).toHaveTextContent('Cannot attach more than 2 files (3 selected).')
    expect(screen.queryByText('1.log')).not.toBeInTheDocument()

    // Per-file size rejection (> 10 bytes)
    fireEvent.change(fileInput, {
      target: { files: [new File(['0123456789A'], 'big.log')] },
    })
    expect(screen.getByRole('alert')).toHaveTextContent('big.log (11 bytes) exceeds the 10 bytes per-file limit.')
    expect(screen.queryByText('big.log')).not.toBeInTheDocument()

    // Valid selection clears alert; cumulative rejection (> 15 bytes) preserves valid file
    fireEvent.change(fileInput, { target: { files: [new File(['0123456789'], 'ten.log')] } })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByText('ten.log')).toBeInTheDocument()

    fireEvent.change(fileInput, { target: { files: [new File(['012345'], 'six.log')] } })
    expect(screen.getByRole('alert')).toHaveTextContent('Selected files total 16 bytes, exceeding the 15 bytes total limit.')
    expect(screen.getByText('ten.log')).toBeInTheDocument()
    expect(screen.queryByText('six.log')).not.toBeInTheDocument()

    // Tighten capability limit before submit so encoder rejects before starting FileReader or POST
    const tighterCaps = makeFeedbackCapabilities(undefined, {
      max_files: 2,
      max_file_bytes: 8,
      max_total_bytes: 15,
    })
    rerender(<FeedbackDialog open={true} onOpenChange={onOpenChange} capabilities={tighterCaps} />)
    readSpy.mockClear()

    await user.click(screen.getByRole('button', { name: /create issue/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('ten.log (10 bytes) exceeds the 8 bytes per-file limit.')
    expect(readSpy).not.toHaveBeenCalled()
    expect(mockPost.mock.calls.filter(([p]) => p === '/api/feedback/create')).toHaveLength(0)
  })
})
