import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { FeedbackDialog } from '../FeedbackDialog'

// Mock the api module
vi.mock('@/lib/api', () => ({
  api: {
    post: vi.fn(),
    get: vi.fn(),
  },
  getRecentFailedCalls: vi.fn(() => []),
  ApiError: class extends Error {
    status: number
    statusText: string
    body: unknown
    constructor(status: number, statusText: string, body: unknown) {
      super(`API error ${status}: ${statusText}`)
      this.status = status
      this.statusText = statusText
      this.body = body
    }
  },
}))

// Mock errorCapture
vi.mock('@/lib/errorCapture', () => ({
  getRecentErrors: vi.fn(() => ['error1', 'error2']),
}))

import { api, getRecentFailedCalls } from '@/lib/api'

const mockPost = api.post as ReturnType<typeof vi.fn>
const mockGetFailedCalls = getRecentFailedCalls as ReturnType<typeof vi.fn>

describe('FeedbackDialog', () => {
  const onOpenChange = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders form with description textarea when open', () => {
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    expect(screen.getByText('Send Feedback')).toBeInTheDocument()
    expect(screen.getByText(/Describe your issue or idea/)).toBeInTheDocument()
    expect(screen.getByLabelText('Description')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('Describe your issue or suggestion...')).toBeInTheDocument()
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

  it('shows error message on create failure', async () => {
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
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
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

  it('Try Again from create error returns to preview', async () => {
    mockPost.mockResolvedValueOnce({
      title: 'AI title',
      body: 'AI body',
      labels: ['bug'],
    })
    mockPost.mockRejectedValueOnce(new Error('Create failed'))

    const user = userEvent.setup()
    render(<FeedbackDialog open={true} onOpenChange={onOpenChange} />)
    await user.type(screen.getByLabelText('Description'), 'Some feedback')
    await user.click(screen.getByRole('button', { name: /preview/i }))
    await waitFor(() => expect(screen.getByLabelText('Title')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /create issue/i }))
    await waitFor(() => expect(screen.getByText(/Create failed/)).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /try again/i }))
    // Should return to preview phase with title/body still intact
    expect(screen.getByText('Preview Issue')).toBeInTheDocument()
    expect(screen.getByLabelText('Title')).toHaveValue('AI title')
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
    expect(link).toHaveAttribute('href', 'https://github.com/myk-org/jenkins-job-insight/issues/new')
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
})
