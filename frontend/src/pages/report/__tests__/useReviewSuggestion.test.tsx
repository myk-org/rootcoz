import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { useEffect } from 'react'
import { useReportDispatch } from '@/pages/report/reportState'
import { ReportProvider } from '../ReportContext'
import { useAiSelection } from '../useAiSelection'
import { useReviewSuggestion } from '../useReviewSuggestion'
import { ConfirmDialog } from '@/components/shared/ConfirmDialog'

/* ------------------------------------------------------------------ */
/*  Mocks                                                              */
/* ------------------------------------------------------------------ */

vi.mock('@/lib/cookies', () => ({
  getUsername: () => 'testuser',
}))

const mockPut = vi.fn()
const mockPost = vi.fn()

vi.mock('@/lib/api', () => ({
  api: {
    put: (...args: unknown[]) => mockPut(...args),
    post: (...args: unknown[]) => mockPost(...args),
    get: vi.fn().mockResolvedValue({ users: [] }),
  },
}))

/* ------------------------------------------------------------------ */
/*  useReviewSuggestion hook integration tests                         */
/* ------------------------------------------------------------------ */

/** Test harness that exposes hook state and actions via rendered UI. */
function HookHarness({ setReviewed }: { setReviewed?: boolean }) {
  const dispatch = useReportDispatch()
  const { showSuggestion, loading, error, maybeSuggest, dismissSuggestion, confirmSuggestion } = useReviewSuggestion({
    jobId: 'job-1',
    testName: 'test-a',
  })

  // Optionally pre-set the review state
  useEffect(() => {
    if (setReviewed) {
      dispatch({
        type: 'SET_REVIEW',
        payload: { key: 'test-a', state: { reviewed: true, username: 'someone', updated_at: new Date().toISOString() } },
      })
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <>
      <span data-testid="show">{String(showSuggestion)}</span>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="error">{error ?? ''}</span>
      <button data-testid="suggest-reviewed" onClick={() => void maybeSuggest('This is a known issue')}>Suggest Reviewed</button>
      <button data-testid="suggest-not-reviewed" onClick={() => void maybeSuggest('Looking into it')}>Suggest Not Reviewed</button>
      <ConfirmDialog
        open={showSuggestion}
        onOpenChange={(open) => { if (!open) dismissSuggestion() }}
        title="Mark as reviewed?"
        description="Would you like to mark it as reviewed?"
        confirmLabel="Yes"
        cancelLabel="No"
        onConfirm={confirmSuggestion}
        loading={loading}
      />
    </>
  )
}

function renderHarness(props: { setReviewed?: boolean } = {}) {
  return render(
    <ReportProvider>
      <HookHarness {...props} />
    </ReportProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockPut.mockResolvedValue({ status: 'ok', reviewed_by: 'testuser' })
})

describe('useReviewSuggestion hook', () => {
  it('does not show suggestion initially', () => {
    renderHarness()
    expect(screen.getByTestId('show').textContent).toBe('false')
  })

  it('shows suggestion when API returns suggests_reviewed: true', async () => {
    mockPost.mockResolvedValueOnce({ suggests_reviewed: true, reason: 'Contains known issue reference' })
    renderHarness()

    await act(async () => {
      fireEvent.click(screen.getByTestId('suggest-reviewed'))
    })

    await waitFor(() => {
      expect(screen.getByTestId('show').textContent).toBe('true')
    })
    expect(mockPost).toHaveBeenCalledWith('/api/analyze-comment-intent', { comment: 'This is a known issue', job_id: 'job-1' })
    expect(screen.getByText('Mark as reviewed?')).toBeDefined()
  })

  it('does not show suggestion when API returns suggests_reviewed: false', async () => {
    mockPost.mockResolvedValueOnce({ suggests_reviewed: false, reason: 'Generic comment' })
    renderHarness()

    await act(async () => {
      fireEvent.click(screen.getByTestId('suggest-not-reviewed'))
    })

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/analyze-comment-intent', { comment: 'Looking into it', job_id: 'job-1' })
    })
    expect(screen.getByTestId('show').textContent).toBe('false')
  })

  it('does not show suggestion when API call fails (safe default)', async () => {
    mockPost.mockRejectedValueOnce(new Error('Network error'))
    renderHarness()

    await act(async () => {
      fireEvent.click(screen.getByTestId('suggest-reviewed'))
    })

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/analyze-comment-intent', { comment: 'This is a known issue', job_id: 'job-1' })
    })
    expect(screen.getByTestId('show').textContent).toBe('false')
  })

  it('surfaces the error when the intent API call fails', async () => {
    mockPost.mockRejectedValueOnce(new Error('Intent check failed'))
    renderHarness()

    await act(async () => {
      fireEvent.click(screen.getByTestId('suggest-reviewed'))
    })

    await waitFor(() => {
      expect(screen.getByTestId('error').textContent).toBe('Intent check failed')
    })
  })

  it('clears a failed intent error when a later analysis succeeds', async () => {
    mockPost.mockRejectedValueOnce(new Error('Intent check failed'))
    renderHarness()

    await act(async () => {
      fireEvent.click(screen.getByTestId('suggest-reviewed'))
    })
    await waitFor(() => {
      expect(screen.getByTestId('error').textContent).toBe('Intent check failed')
    })

    mockPost.mockResolvedValueOnce({ suggests_reviewed: false, reason: 'Generic comment' })
    await act(async () => {
      fireEvent.click(screen.getByTestId('suggest-not-reviewed'))
    })

    await waitFor(() => {
      expect(screen.getByTestId('error').textContent).toBe('')
    })
    expect(mockPost).toHaveBeenCalledTimes(2)
  })

  it('does not call API when already reviewed', async () => {
    renderHarness({ setReviewed: true })
    // Wait for the SET_REVIEW dispatch to take effect
    await waitFor(() => {
      expect(screen.getByTestId('show').textContent).toBe('false')
    })

    await act(async () => {
      fireEvent.click(screen.getByTestId('suggest-reviewed'))
    })

    expect(mockPost).not.toHaveBeenCalled()
    expect(screen.getByTestId('show').textContent).toBe('false')
  })

  it('dismisses suggestion when No is clicked', async () => {
    mockPost.mockResolvedValueOnce({ suggests_reviewed: true, reason: 'Match' })
    renderHarness()

    await act(async () => {
      fireEvent.click(screen.getByTestId('suggest-reviewed'))
    })

    await waitFor(() => {
      expect(screen.getByTestId('show').textContent).toBe('true')
    })

    fireEvent.click(screen.getByRole('button', { name: 'No' }))
    expect(screen.getByTestId('show').textContent).toBe('false')
  })

  it('calls review API and hides dialog when Yes is clicked', async () => {
    mockPost.mockResolvedValueOnce({ suggests_reviewed: true, reason: 'Match' })
    renderHarness()

    await act(async () => {
      fireEvent.click(screen.getByTestId('suggest-reviewed'))
    })

    await waitFor(() => {
      expect(screen.getByTestId('show').textContent).toBe('true')
    })

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Yes' }))
    })

    await waitFor(() => {
      expect(mockPut).toHaveBeenCalledWith('/results/job-1/reviewed', {
        test_name: 'test-a',
        reviewed: true,
        child_job_name: '',
        child_build_number: 0,
      })
    })

    expect(screen.getByTestId('show').textContent).toBe('false')
  })

  it('sets error when review API call fails', async () => {
    mockPost.mockResolvedValueOnce({ suggests_reviewed: true, reason: 'Match' })
    mockPut.mockRejectedValueOnce(new Error('Network error'))
    renderHarness()

    await act(async () => {
      fireEvent.click(screen.getByTestId('suggest-reviewed'))
    })

    await waitFor(() => {
      expect(screen.getByTestId('show').textContent).toBe('true')
    })

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Yes' }))
    })

    await waitFor(() => {
      expect(screen.getByTestId('error').textContent).toBe('Network error')
    })
    expect(screen.getByTestId('show').textContent).toBe('false')
  })
})

/* ------------------------------------------------------------------ */
/*  Out-of-order responses                                             */
/* ------------------------------------------------------------------ */

/** A promise a test settles by hand, to interleave two in-flight requests. */
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe('useReviewSuggestion – out-of-order intent responses', () => {
  it('keeps the newest error when an older success resolves late', async () => {
    const first = deferred<{ suggests_reviewed: boolean; reason: string }>()
    const second = deferred<{ suggests_reviewed: boolean; reason: string }>()
    mockPost.mockImplementationOnce(() => first.promise).mockImplementationOnce(() => second.promise)
    renderHarness()

    fireEvent.click(screen.getByTestId('suggest-reviewed'))
    fireEvent.click(screen.getByTestId('suggest-not-reviewed'))

    // The newer request fails first and the message lands.
    await act(async () => {
      second.reject(new Error('Intent check failed'))
    })
    await waitFor(() => {
      expect(screen.getByTestId('error').textContent).toBe('Intent check failed')
    })

    // The older request now succeeds; it must not wipe the newer failure.
    await act(async () => {
      first.resolve({ suggests_reviewed: false, reason: 'Generic comment' })
    })
    expect(screen.getByTestId('error').textContent).toBe('Intent check failed')
  })

  it('keeps the newest error when an older suggestion resolves late', async () => {
    const first = deferred<{ suggests_reviewed: boolean; reason: string }>()
    const second = deferred<{ suggests_reviewed: boolean; reason: string }>()
    mockPost.mockImplementationOnce(() => first.promise).mockImplementationOnce(() => second.promise)
    renderHarness()

    fireEvent.click(screen.getByTestId('suggest-reviewed'))
    fireEvent.click(screen.getByTestId('suggest-not-reviewed'))

    await act(async () => {
      second.reject(new Error('Intent check failed'))
    })
    await waitFor(() => {
      expect(screen.getByTestId('error').textContent).toBe('Intent check failed')
    })

    // A stale "yes, reviewed" must not raise the dialog on top of the newer failure.
    await act(async () => {
      first.resolve({ suggests_reviewed: true, reason: 'Known issue' })
    })
    expect(screen.getByTestId('show').textContent).toBe('false')
    expect(screen.getByTestId('error').textContent).toBe('Intent check failed')
  })

  it('does not let an older success re-raise a dismissed suggestion the newer request refused', async () => {
    const first = deferred<{ suggests_reviewed: boolean; reason: string }>()
    const second = deferred<{ suggests_reviewed: boolean; reason: string }>()
    mockPost.mockImplementationOnce(() => first.promise).mockImplementationOnce(() => second.promise)
    renderHarness()

    fireEvent.click(screen.getByTestId('suggest-reviewed'))
    fireEvent.click(screen.getByTestId('suggest-not-reviewed'))

    await act(async () => {
      first.resolve({ suggests_reviewed: true, reason: 'Known issue' })
    })
    expect(screen.getByTestId('show').textContent).toBe('false')

    // Newest request succeeds without a suggestion; the stale one must stay ignored.
    await act(async () => {
      second.resolve({ suggests_reviewed: false, reason: 'Generic comment' })
    })
    expect(screen.getByTestId('show').textContent).toBe('false')
  })

  it('ignores a stale failure that lands before the newer request settles', async () => {
    const first = deferred<{ suggests_reviewed: boolean; reason: string }>()
    const second = deferred<{ suggests_reviewed: boolean; reason: string }>()
    mockPost.mockImplementationOnce(() => first.promise).mockImplementationOnce(() => second.promise)
    renderHarness()

    fireEvent.click(screen.getByTestId('suggest-reviewed'))
    fireEvent.click(screen.getByTestId('suggest-not-reviewed'))

    await act(async () => {
      first.reject(new Error('Intent check failed'))
    })

    // Newest request succeeds: it owns the error slot, so nothing stale shows through.
    await act(async () => {
      second.resolve({ suggests_reviewed: false, reason: 'Generic comment' })
    })
    expect(screen.getByTestId('error').textContent).toBe('')
    expect(screen.getByTestId('show').textContent).toBe('false')
  })
})

/* ------------------------------------------------------------------ */
/*  AI pair selection (report-scoped, shared by every AI surface)     */
/* ------------------------------------------------------------------ */

function AiHarness() {
  const { aiProvider, aiModel, setAiPair } = useAiSelection('openai', 'gpt-4o')
  const { maybeSuggest } = useReviewSuggestion({ jobId: 'job-1', testName: 'test-a' })
  return (
    <>
      <span>{aiProvider}/{aiModel}</span>
      <button data-testid="pick" onClick={() => setAiPair('claude', 'sonnet')}>pick</button>
      <button data-testid="pick-provider-only" onClick={() => setAiPair('claude', '')}>pick provider only</button>
      <button data-testid="suggest" onClick={() => void maybeSuggest('fixed in PR #1')}>suggest</button>
    </>
  )
}

describe('report-scoped AI selection', () => {
  beforeEach(() => {
    mockPost.mockResolvedValue({ suggests_reviewed: false, reason: '' })
  })

  it('sends the selected pair to analyze-comment-intent', async () => {
    render(<ReportProvider><AiHarness /></ReportProvider>)
    fireEvent.click(screen.getByTestId('pick'))
    await act(async () => {
      fireEvent.click(screen.getByTestId('suggest'))
    })
    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/analyze-comment-intent', {
        comment: 'fixed in PR #1',
        job_id: 'job-1',
        ai_provider: 'claude',
        ai_model: 'sonnet',
      })
    })
  })

  it('omits the pair when a provider is chosen without a model', async () => {
    render(<ReportProvider><AiHarness /></ReportProvider>)
    fireEvent.click(screen.getByTestId('pick-provider-only'))
    await act(async () => {
      fireEvent.click(screen.getByTestId('suggest'))
    })
    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/api/analyze-comment-intent', {
        comment: 'fixed in PR #1',
        job_id: 'job-1',
      })
    })
  })

  it('keeps the last choice on later surfaces instead of reverting to their default', async () => {
    render(
      <ReportProvider>
        <AiHarness />
        <AiHarness />
      </ReportProvider>,
    )
    fireEvent.click(screen.getAllByTestId('pick')[0])
    await waitFor(() => {
      expect(screen.getAllByText('claude/sonnet')).toHaveLength(2)
    })
    // Both surfaces read the same page-scoped pair; neither falls back to its default.
    expect(screen.queryByText('openai/gpt-4o')).toBeNull()
  })
})
