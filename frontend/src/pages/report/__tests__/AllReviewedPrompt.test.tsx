import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { useEffect, useImperativeHandle, forwardRef, createRef } from 'react'
import { AllReviewedPrompt } from '../AllReviewedPrompt'
import { ReportProvider, useReportDispatch } from '../ReportContext'
import type { AnalysisResult, ReviewState } from '@/types'

/* ------------------------------------------------------------------ */
/*  Mocks                                                              */
/* ------------------------------------------------------------------ */

const mockPost = vi.fn()

vi.mock('@/lib/api', () => ({
  api: {
    post: (...args: unknown[]) => mockPost(...args),
  },
}))

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function makeResult(overrides: Partial<AnalysisResult> = {}): AnalysisResult {
  return {
    job_id: 'job-1',
    job_name: 'test-job',
    build_number: 1,
    jenkins_url: null,
    status: 'completed',
    summary: 'Test summary',
    ai_provider: 'test',
    ai_model: 'test-model',
    failures: [
      {
        test_name: 'test-a',
        error: 'error-a',
        analysis: { classification: 'PRODUCT BUG', affected_tests: ['test-a'], details: '', artifacts_evidence: '' },
        error_signature: 'sig-a',
      },
      {
        test_name: 'test-b',
        error: 'error-b',
        analysis: { classification: 'CODE ISSUE', affected_tests: ['test-b'], details: '', artifacts_evidence: '' },
        error_signature: 'sig-b',
      },
    ],
    child_job_analyses: [],
    ...overrides,
  }
}

function makeReview(reviewed: boolean): ReviewState {
  return { reviewed, username: 'user', updated_at: '2025-01-01T00:00:00Z' }
}

/**
 * Helper that injects state into ReportContext, then renders AllReviewedPrompt.
 * Dispatches initial state, then allows further review updates via onMount callback.
 */
function Injector({
  result,
  reviews,
  reportportalAvailable,
  additionalReviews,
}: {
  result: AnalysisResult
  reviews: Record<string, ReviewState>
  reportportalAvailable: boolean
  additionalReviews?: Record<string, ReviewState>
}) {
  const dispatch = useReportDispatch()
  useEffect(() => {
    dispatch({
      type: 'SET_RESULT',
      payload: { result, createdAt: '', completedAt: '', analysisStartedAt: '' },
    })
    dispatch({ type: 'SET_REPORTPORTAL_AVAILABLE', payload: reportportalAvailable })
    dispatch({ type: 'SET_COMMENTS_AND_REVIEWS', payload: { comments: [], reviews } })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Dispatch additional reviews after a tick to simulate a real user action
  // (must happen in a separate render cycle from the initial data load)
  useEffect(() => {
    if (additionalReviews) {
      const id = setTimeout(() => {
        for (const [key, state] of Object.entries(additionalReviews)) {
          dispatch({ type: 'SET_REVIEW', payload: { key, state } })
        }
        // Fire event in a separate tick so React processes the SET_REVIEW state updates first
        setTimeout(() => window.dispatchEvent(new CustomEvent('jji:review-changed', { detail: { jobId: 'job-1' } })), 0)
      }, 0)
      return () => clearTimeout(id)
    }
  }, [additionalReviews]) // eslint-disable-line react-hooks/exhaustive-deps

  return <AllReviewedPrompt jobId="job-1" />
}

function renderPrompt(opts: {
  result?: AnalysisResult
  reviews?: Record<string, ReviewState>
  reportportalAvailable?: boolean
  additionalReviews?: Record<string, ReviewState>
}) {
  return render(
    <ReportProvider>
      <Injector
        result={opts.result ?? makeResult()}
        reviews={opts.reviews ?? {}}
        reportportalAvailable={opts.reportportalAvailable ?? true}
        additionalReviews={opts.additionalReviews}
      />
    </ReportProvider>,
  )
}

/** Imperative handle exposed by DynamicInjector for dispatching reviews on demand. */
interface DynamicInjectorHandle {
  setReviews: (reviews: Record<string, ReviewState>) => void
}

const DynamicInjector = forwardRef<DynamicInjectorHandle, {
  result: AnalysisResult
  reviews: Record<string, ReviewState>
  reportportalAvailable: boolean
}>(function DynamicInjector({ result, reviews, reportportalAvailable }, ref) {
  const dispatch = useReportDispatch()
  useEffect(() => {
    dispatch({
      type: 'SET_RESULT',
      payload: { result, createdAt: '', completedAt: '', analysisStartedAt: '' },
    })
    dispatch({ type: 'SET_REPORTPORTAL_AVAILABLE', payload: reportportalAvailable })
    dispatch({ type: 'SET_COMMENTS_AND_REVIEWS', payload: { comments: [], reviews } })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useImperativeHandle(ref, () => ({
    setReviews(newReviews: Record<string, ReviewState>) {
      for (const [key, state] of Object.entries(newReviews)) {
        dispatch({ type: 'SET_REVIEW', payload: { key, state } })
      }
      window.dispatchEvent(new CustomEvent('jji:review-changed', { detail: { jobId: 'job-1' } }))
    },
  }))

  return <AllReviewedPrompt jobId="job-1" />
})

/* ------------------------------------------------------------------ */
/*  Tests                                                              */
/* ------------------------------------------------------------------ */

beforeEach(() => {
  vi.clearAllMocks()
  mockPost.mockResolvedValue({})
})

describe('AllReviewedPrompt', () => {
  it('shows dialog when all failures transition to reviewed', async () => {
    renderPrompt({
      reviews: { 'test-a': makeReview(false) },
      additionalReviews: {
        'test-a': makeReview(true),
        'test-b': makeReview(true),
      },
    })

    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Report Portal?')).toBeDefined()
    })
  })

  it('does not show dialog when reportportal is not available', async () => {
    renderPrompt({
      reportportalAvailable: false,
      additionalReviews: {
        'test-a': makeReview(true),
        'test-b': makeReview(true),
      },
    })

    // Wait a tick to ensure effects have run
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('All failures reviewed. Update Report Portal?')).toBeNull()
  })

  it('does not show dialog when not all failures are reviewed', async () => {
    renderPrompt({
      additionalReviews: {
        'test-a': makeReview(true),
        // test-b not reviewed
      },
    })

    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('All failures reviewed. Update Report Portal?')).toBeNull()
  })

  it('calls push-reportportal API on confirm', async () => {
    renderPrompt({
      additionalReviews: {
        'test-a': makeReview(true),
        'test-b': makeReview(true),
      },
    })

    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Report Portal?')).toBeDefined()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Yes' }))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/results/job-1/push-reportportal')
    })
  })

  it('dismisses dialog on No without calling API', async () => {
    renderPrompt({
      additionalReviews: {
        'test-a': makeReview(true),
        'test-b': makeReview(true),
      },
    })

    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Report Portal?')).toBeDefined()
    })

    fireEvent.click(screen.getByRole('button', { name: 'No' }))

    await waitFor(() => {
      expect(screen.queryByText('All failures reviewed. Update Report Portal?')).toBeNull()
    })
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('does not show dialog when no failures exist', async () => {
    renderPrompt({
      result: makeResult({ failures: [], child_job_analyses: [] }),
    })

    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('All failures reviewed. Update Report Portal?')).toBeNull()
  })

  it('includes child job failures in the all-reviewed check', async () => {
    const result = makeResult({
      failures: [
        {
          test_name: 'parent-test',
          error: 'err',
          analysis: { classification: 'PRODUCT BUG', affected_tests: ['parent-test'], details: '', artifacts_evidence: '' },
          error_signature: 'sig-p',
        },
      ],
      child_job_analyses: [
        {
          job_name: 'child-job',
          build_number: 42,
          jenkins_url: null,
          summary: null,
          failures: [
            {
              test_name: 'child-test',
              error: 'err',
              analysis: { classification: 'CODE ISSUE', affected_tests: ['child-test'], details: '', artifacts_evidence: '' },
              error_signature: 'sig-c',
            },
          ],
          failed_children: [],
          note: null,
        },
      ],
    })

    // Only review parent test — should NOT trigger
    const { unmount } = renderPrompt({
      result,
      additionalReviews: {
        'parent-test': makeReview(true),
      },
    })

    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('All failures reviewed. Update Report Portal?')).toBeNull()
    unmount()

    // Review both parent and child — should trigger
    renderPrompt({
      result,
      additionalReviews: {
        'parent-test': makeReview(true),
        'child-job#42::child-test': makeReview(true),
      },
    })

    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Report Portal?')).toBeDefined()
    })
  })

  it('does not show dialog when report loads with all failures already reviewed', async () => {
    renderPrompt({
      reviews: {
        'test-a': makeReview(true),
        'test-b': makeReview(true),
      },
    })

    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('All failures reviewed. Update Report Portal?')).toBeNull()
  })

  it('re-triggers dialog after cycle: all-reviewed → one unreviewed → all-reviewed', async () => {
    const injectorRef = createRef<DynamicInjectorHandle>()

    render(
      <ReportProvider>
        <DynamicInjector
          ref={injectorRef}
          result={makeResult()}
          reviews={{}}
          reportportalAvailable={true}
        />
      </ReportProvider>,
    )

    // Step 1: Mark all as reviewed → dialog should open
    await act(async () => {
      injectorRef.current!.setReviews({
        'test-a': makeReview(true),
        'test-b': makeReview(true),
      })
    })

    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Report Portal?')).toBeDefined()
    })

    // Dismiss dialog
    fireEvent.click(screen.getByRole('button', { name: 'No' }))
    await waitFor(() => {
      expect(screen.queryByText('All failures reviewed. Update Report Portal?')).toBeNull()
    })

    // Step 2: Un-review one failure
    await act(async () => {
      injectorRef.current!.setReviews({
        'test-a': makeReview(false),
      })
    })

    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('All failures reviewed. Update Report Portal?')).toBeNull()

    // Step 3: Mark all reviewed again → dialog should re-open
    await act(async () => {
      injectorRef.current!.setReviews({
        'test-a': makeReview(true),
        'test-b': makeReview(true),
      })
    })

    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Report Portal?')).toBeDefined()
    })
  })

  it('dialog closes after API error (best-effort)', async () => {
    mockPost.mockRejectedValueOnce(new Error('Network error'))

    renderPrompt({
      additionalReviews: {
        'test-a': makeReview(true),
        'test-b': makeReview(true),
      },
    })

    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Report Portal?')).toBeDefined()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Yes' }))

    await waitFor(() => {
      expect(screen.queryByText('All failures reviewed. Update Report Portal?')).toBeNull()
    })
  })
})
