import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { useEffect } from 'react'
import { ReportProvider, useReportDispatch } from '../ReportContext'
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
