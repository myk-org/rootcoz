import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { FeedbackDialog } from '../FeedbackDialog'

// The account row is the dialog's only credential source.
const mockUserTokens = vi.fn(() => ({
  github_token: 'ghp_test_token',
  jira_email: '',
  jira_token: '',
  jira_sa_token: '',
}))

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
      get: vi.fn((path?: string) =>
        Promise.resolve(
          path === '/api/user/tokens'
            ? mockUserTokens()
            : { providers: {}, provider_status: {} },
        ),
      ),
    },
    getRecentFailedCalls: vi.fn(() => []),
    ApiError: _ApiError,
    extractApiDetail: (err: unknown): string | null => {
      if (err instanceof _ApiError) {
        const detail = (err.body as { detail?: string })?.detail
        if (detail) return detail
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
