import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { useEffect, useImperativeHandle, forwardRef, createRef } from 'react'
import { AllReviewedPrompt } from '../AllReviewedPrompt'
import { useReportDispatch } from '@/pages/report/reportState'
import { ReportProvider } from '../ReportContext'
import { ApiError } from '@/lib/api'
import type { AnalysisResult, ChildJobAnalysis, ReviewState } from '@/types'

/* ------------------------------------------------------------------ */
/*  Mocks                                                              */
/* ------------------------------------------------------------------ */

const mockPost = vi.fn()

// Keep the real module (ApiError, extractApiDetail) so rejection-reason
// extraction is exercised exactly as in production; only api.post is mocked.
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      post: (...args: unknown[]) => mockPost(...args),
    },
  }
})

const mockAuth = {
  username: 'testuser',
  isAdmin: false,
  isOperator: true,
  canViewReports: true,
  canUseServerProviders: false,
  canUseFreeProviders: false,
  role: 'operator',
  loading: false,
  authenticated: true,
  login: vi.fn(),
  logout: vi.fn(),
  refreshAuth: vi.fn(),
}

vi.mock('@/lib/auth', () => ({
  useAuth: () => mockAuth,
}))

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function makeAnalysis(
  classification: string,
  affected: string[],
): AnalysisResult['failures'][number]['analysis'] {
  return {
    classification,
    pattern: '',
    affected_tests: affected,
    details: '',
    artifacts_evidence: '',
  }
}

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
        id: 'f-a',
        test_name: 'test-a',
        error: 'error-a',
        analysis: makeAnalysis('PRODUCT BUG', ['test-a']),
        error_signature: 'sig-a',
      },
      {
        id: 'f-b',
        test_name: 'test-b',
        error: 'error-b',
        analysis: makeAnalysis('CODE ISSUE', ['test-b']),
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

function makeChildJob(overrides: Partial<ChildJobAnalysis> = {}): ChildJobAnalysis {
  return {
    id: 'child-1',
    job_name: 'child-job',
    build_number: 42,
    jenkins_url: null,
    summary: null,
    failures: [
      {
        id: 'f-child',
        test_name: 'child-test',
        error: 'err',
        analysis: makeAnalysis('CODE ISSUE', ['child-test']),
        error_signature: 'sig-c',
      },
    ],
    failed_children: [],
    note: null,
    ...overrides,
  }
}

/**
 * Helper that injects state into ReportContext, then renders AllReviewedPrompt.
 * Dispatches initial state, then allows further review updates via onMount callback.
 */
function Injector({
  jobId = 'job-1',
  result,
  reviews,
  reportportalAvailable,
  datadogAvailable,
  additionalReviews,
}: {
  jobId?: string
  result: AnalysisResult
  reviews: Record<string, ReviewState>
  reportportalAvailable: boolean
  datadogAvailable?: boolean
  additionalReviews?: Record<string, ReviewState>
}) {
  const dispatch = useReportDispatch()
  useEffect(() => {
    dispatch({
      type: 'SET_RESULT',
      payload: { result, createdAt: '', completedAt: '', analysisStartedAt: '' },
    })
    dispatch({ type: 'SET_REPORTPORTAL_AVAILABLE', payload: reportportalAvailable })
    dispatch({ type: 'SET_DATADOG_AVAILABLE', payload: datadogAvailable ?? false })
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
        setTimeout(() => window.dispatchEvent(new CustomEvent('rootcoz:review-changed', { detail: { jobId: 'job-1' } })), 0)
      }, 0)
      return () => clearTimeout(id)
    }
  }, [additionalReviews]) // eslint-disable-line react-hooks/exhaustive-deps

  return <AllReviewedPrompt jobId={jobId} />
}

function renderPrompt(opts: {
  jobId?: string
  result?: AnalysisResult
  reviews?: Record<string, ReviewState>
  reportportalAvailable?: boolean
  datadogAvailable?: boolean
  additionalReviews?: Record<string, ReviewState>
}) {
  return render(
    <ReportProvider>
      <Injector
        jobId={opts.jobId}
        result={opts.result ?? makeResult()}
        reviews={opts.reviews ?? {}}
        reportportalAvailable={opts.reportportalAvailable ?? true}
        datadogAvailable={opts.datadogAvailable}
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
      window.dispatchEvent(new CustomEvent('rootcoz:review-changed', { detail: { jobId: 'job-1' } }))
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
  mockAuth.isOperator = true
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

  it('does not show dialog when neither reportportal nor datadog is available', async () => {
    renderPrompt({
      reportportalAvailable: false,
      datadogAvailable: false,
      additionalReviews: {
        'test-a': makeReview(true),
        'test-b': makeReview(true),
      },
    })

    // Wait a tick to ensure effects have run
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('All failures reviewed. Update Report Portal?')).toBeNull()
    expect(screen.queryByText('All failures reviewed. Update Datadog?')).toBeNull()
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

  it('calls the generic reportportal push API on confirm, never the legacy endpoint', async () => {
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
      expect(mockPost).toHaveBeenCalledWith('/results/job-1/push/reportportal')
    })
    // Guard: reintroducing a direct legacy `push-reportportal` call must fail
    expect(mockPost.mock.calls.some((c) => String(c[0]).includes('push-reportportal'))).toBe(false)
  })

  it('pushes the parent scope and each child scope when child jobs have failures', async () => {
    renderPrompt({
      result: makeResult({
        failures: [
          {
            id: 'f-parent',
            test_name: 'parent-test',
            error: 'err',
            analysis: makeAnalysis('PRODUCT BUG', ['parent-test']),
            error_signature: 'sig-p',
          },
        ],
        child_job_analyses: [makeChildJob()],
      }),
      additionalReviews: {
        'parent-test': makeReview(true),
        'child-job#42::child-test': makeReview(true),
      },
    })

    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Report Portal?')).toBeDefined()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Yes' }))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/results/job-1/push/reportportal')
      expect(mockPost).toHaveBeenCalledWith(
        '/results/job-1/push/reportportal?child_job_name=child-job&child_build_number=42',
      )
    })
  })

  it('pushes only child scopes when failures exist only in child jobs', async () => {
    renderPrompt({
      result: makeResult({
        failures: [],
        child_job_analyses: [makeChildJob()],
      }),
      additionalReviews: {
        'child-job#42::child-test': makeReview(true),
      },
    })

    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Report Portal?')).toBeDefined()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Yes' }))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledTimes(1)
      expect(mockPost).toHaveBeenCalledWith(
        '/results/job-1/push/reportportal?child_job_name=child-job&child_build_number=42',
      )
    })
    expect(mockPost).not.toHaveBeenCalledWith('/results/job-1/push/reportportal')
  })

  it('pushes child scopes to datadog too when both exporters are available', async () => {
    renderPrompt({
      result: makeResult({
        failures: [],
        child_job_analyses: [makeChildJob()],
      }),
      reportportalAvailable: true,
      datadogAvailable: true,
      additionalReviews: {
        'child-job#42::child-test': makeReview(true),
      },
    })

    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Report Portal and Datadog?')).toBeDefined()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Yes' }))

    const childQuery = '?child_job_name=child-job&child_build_number=42'
    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith(`/results/job-1/push/reportportal${childQuery}`)
      expect(mockPost).toHaveBeenCalledWith(`/results/job-1/push/datadog${childQuery}`)
    })
  })

  it('does not show the dialog for non-operators even when all failures are reviewed', async () => {
    mockAuth.isOperator = false

    renderPrompt({
      additionalReviews: {
        'test-a': makeReview(true),
        'test-b': makeReview(true),
      },
    })

    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('All failures reviewed. Update Report Portal?')).toBeNull()
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('calls push datadog API on confirm when only datadog is available', async () => {
    renderPrompt({
      reportportalAvailable: false,
      datadogAvailable: true,
      additionalReviews: {
        'test-a': makeReview(true),
        'test-b': makeReview(true),
      },
    })

    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Datadog?')).toBeDefined()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Yes' }))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/results/job-1/push/datadog')
    })
    expect(mockPost).not.toHaveBeenCalledWith('/results/job-1/push/reportportal')
  })

  it('pushes to both reportportal and datadog when both are available', async () => {
    renderPrompt({
      reportportalAvailable: true,
      datadogAvailable: true,
      additionalReviews: {
        'test-a': makeReview(true),
        'test-b': makeReview(true),
      },
    })

    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Report Portal and Datadog?')).toBeDefined()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Yes' }))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/results/job-1/push/reportportal')
      expect(mockPost).toHaveBeenCalledWith('/results/job-1/push/datadog')
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
          id: 'f-parent',
          test_name: 'parent-test',
          error: 'err',
          analysis: makeAnalysis('PRODUCT BUG', ['parent-test']),
          error_signature: 'sig-p',
        },
      ],
      child_job_analyses: [
        {
          id: 'child-1',
          job_name: 'child-job',
          build_number: 42,
          jenkins_url: null,
          summary: null,
          failures: [
            {
              id: 'f-child',
              test_name: 'child-test',
              error: 'err',
              analysis: makeAnalysis('CODE ISSUE', ['child-test']),
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

  it('surfaces the rejection reason from the API error detail', async () => {
    mockPost.mockRejectedValueOnce(
      new ApiError(403, 'Forbidden', { detail: 'Viewer role cannot push' }),
    )

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
      expect(screen.getByRole('alert')).toBeDefined()
    })
    expect(screen.getByRole('alert').textContent).toContain('Viewer role cannot push')
    // Dialog stays open so the failure is not silently discarded
    expect(screen.getByText('All failures reviewed. Update Report Portal?')).toBeDefined()
  })

  it('falls back to the error message when the API error has no detail', async () => {
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
      expect(screen.getByRole('alert')).toBeDefined()
    })
    expect(screen.getByRole('alert').textContent).toContain('Network error')
    expect(screen.getByText('All failures reviewed. Update Report Portal?')).toBeDefined()
  })

  it('treats unmatched Report Portal tests as a failure and keeps the dialog open', async () => {
    mockPost.mockResolvedValueOnce({
      pushed: 0,
      total: 2,
      unmatched: ['test-a', 'test-b'],
      errors: [],
      launch_id: null,
    })

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
      expect(screen.getByRole('alert')).toBeDefined()
    })
    expect(screen.getByRole('alert').textContent).toContain(
      '2 test(s) could not be matched to Report Portal items',
    )
    expect(screen.getByText('All failures reviewed. Update Report Portal?')).toBeDefined()
  })

  it('retries only the failed scope/exporter combinations', async () => {
    renderPrompt({
      reportportalAvailable: true,
      datadogAvailable: true,
      additionalReviews: {
        'test-a': makeReview(true),
        'test-b': makeReview(true),
      },
    })

    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Report Portal and Datadog?')).toBeDefined()
    })

    // First attempt: Report Portal succeeds, Datadog is rejected
    mockPost.mockResolvedValueOnce({ pushed: 2, unmatched: [], errors: [], launch_id: 5 })
    mockPost.mockRejectedValueOnce(
      new ApiError(502, 'Bad Gateway', { detail: 'Datadog unavailable' }),
    )

    fireEvent.click(screen.getByRole('button', { name: 'Yes' }))

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeDefined()
    })
    expect(mockPost).toHaveBeenCalledTimes(2)
    expect(mockPost).toHaveBeenNthCalledWith(1, '/results/job-1/push/reportportal')
    expect(mockPost).toHaveBeenNthCalledWith(2, '/results/job-1/push/datadog')
    expect(screen.getByRole('alert').textContent).toContain('Datadog unavailable')

    // Retry: only the failed Datadog push is re-sent
    mockPost.mockResolvedValueOnce({
      success: true,
      message: 'ok',
      pushed: 2,
      total: 2,
      errors: [],
      skipped: 0,
    })
    fireEvent.click(screen.getByRole('button', { name: 'Yes' }))

    await waitFor(() => {
      expect(screen.queryByText('All failures reviewed. Update Report Portal and Datadog?')).toBeNull()
    })
    expect(mockPost).toHaveBeenCalledTimes(3)
    expect(mockPost).toHaveBeenNthCalledWith(3, '/results/job-1/push/datadog')
    expect(mockPost.mock.calls.filter((c) => c[0] === '/results/job-1/push/reportportal')).toHaveLength(1)
  })

  it('clears pushError when the dialog is closed and reopened', async () => {
    mockPost.mockRejectedValueOnce(
      new ApiError(403, 'Forbidden', { detail: 'push rejected' }),
    )

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
      expect(screen.getByRole('alert')).toBeDefined()
    })

    // Close via "No" — the error must be discarded
    fireEvent.click(screen.getByRole('button', { name: 'No' }))
    await waitFor(() => {
      expect(screen.queryByText('All failures reviewed. Update Report Portal?')).toBeNull()
    })

    // Re-trigger the dialog — the old red message must not reappear
    await act(async () => {
      window.dispatchEvent(new CustomEvent('rootcoz:review-changed', { detail: { jobId: 'job-1' } }))
    })
    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Report Portal?')).toBeDefined()
    })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('does not set pushError when a push completes after the job changed', async () => {
    const views = renderPrompt({
      jobId: 'job-1',
      additionalReviews: {
        'test-a': makeReview(true),
        'test-b': makeReview(true),
      },
    })

    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Report Portal?')).toBeDefined()
    })

    let rejectPush!: (err: unknown) => void
    mockPost.mockImplementationOnce(
      () => new Promise((_resolve, reject) => { rejectPush = reject }),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Yes' }))

    // Navigate to another job while the push is still in flight
    views.rerender(
      <ReportProvider>
        <Injector
          jobId="job-2"
          result={makeResult()}
          reviews={{}}
          reportportalAvailable={true}
        />
      </ReportProvider>,
    )
    await waitFor(() => {
      expect(screen.queryByText('All failures reviewed. Update Report Portal?')).toBeNull()
    })

    // The stale job-1 push now fails — it must not write state
    await act(async () => {
      rejectPush(new ApiError(403, 'Forbidden', { detail: 'old job failure' }))
    })
    await new Promise((r) => setTimeout(r, 50))

    // job-2's reviews are all set — open its dialog via the event
    await act(async () => {
      window.dispatchEvent(new CustomEvent('rootcoz:review-changed', { detail: { jobId: 'job-2' } }))
    })
    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Report Portal?')).toBeDefined()
    })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('pushes duplicate child scopes only once', async () => {
    const dupChild = (): ChildJobAnalysis =>
      makeChildJob({ job_name: 'dup-job', build_number: 7 })

    renderPrompt({
      result: makeResult({
        failures: [],
        child_job_analyses: [
          dupChild(),
          makeChildJob({
            job_name: 'other-job',
            build_number: 8,
            failed_children: [dupChild()],
          }),
        ],
      }),
      additionalReviews: {
        'dup-job#7::child-test': makeReview(true),
        'other-job#8::child-test': makeReview(true),
      },
    })

    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Report Portal?')).toBeDefined()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Yes' }))

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledTimes(2)
    })
    expect(mockPost).toHaveBeenCalledWith(
      '/results/job-1/push/reportportal?child_job_name=dup-job&child_build_number=7',
    )
    expect(mockPost).toHaveBeenCalledWith(
      '/results/job-1/push/reportportal?child_job_name=other-job&child_build_number=8',
    )
    expect(
      mockPost.mock.calls.filter(
        (c) => c[0] === '/results/job-1/push/reportportal?child_job_name=dup-job&child_build_number=7',
      ),
    ).toHaveLength(1)
  })

  it('surfaces failed (success=false) push responses and keeps the dialog open', async () => {
    mockPost.mockResolvedValueOnce({
      success: false,
      message: 'Datadog API key rejected',
      pushed: 0,
      total: 0,
      errors: [],
      skipped: 0,
    })

    renderPrompt({
      reportportalAvailable: false,
      datadogAvailable: true,
      additionalReviews: {
        'test-a': makeReview(true),
        'test-b': makeReview(true),
      },
    })

    await waitFor(() => {
      expect(screen.getByText('All failures reviewed. Update Datadog?')).toBeDefined()
    })

    fireEvent.click(screen.getByRole('button', { name: 'Yes' }))

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeDefined()
    })
    expect(screen.getByRole('alert').textContent).toContain('Datadog API key rejected')
    expect(screen.getByText('All failures reviewed. Update Datadog?')).toBeDefined()
  })

  it('closes the dialog only when all pushes succeed', async () => {
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
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('AllReviewedPrompt session reset', () => {
  // Both exporters enabled, so a successful Report Portal push leaves the
  // dialog OPEN with the Greenwave offer: that is the state in which a stale
  // outcome can survive into the next session and hide the Yes/No buttons.
  // (With only Report Portal enabled the push closes the dialog through
  // handleOpenChange, which clears the outcome by itself — a test in that
  // shape passes without the reset and proves nothing.)
  const BOTH_EXPORTERS = [
    { name: 'reportportal', display_name: 'Report Portal', enabled: true },
    { name: 'greenwave', display_name: 'Greenwave', enabled: true },
  ]

  function Seed({ exporters }: { exporters: { name: string; display_name: string; enabled: boolean }[] }) {
    const dispatch = useReportDispatch()
    useEffect(() => {
      dispatch({ type: 'SET_EXPORTERS', payload: exporters })
      dispatch({ type: 'SET_GREENWAVE_WAIVERS_ENABLED', payload: false })
    }, []) // eslint-disable-line react-hooks/exhaustive-deps
    return null
  }

  it('offers the answer again on a new all-reviewed session', async () => {
    render(
      <ReportProvider>
        <Seed exporters={BOTH_EXPORTERS} />
        <Injector
          jobId="job-1"
          result={makeResult()}
          reviews={{}}
          reportportalAvailable={true}
          additionalReviews={{
            'test-a': makeReview(true),
            'test-b': makeReview(true),
          }}
        />
      </ReportProvider>,
    )

    await waitFor(() => expect(screen.getByRole('button', { name: 'Yes' })).toBeDefined())
    mockPost.mockResolvedValueOnce({ success: true, message: 'ok', pushed: 2 })
    fireEvent.click(screen.getByRole('button', { name: 'Yes' }))

    // The dialog stays open (Greenwave still on offer) with the outcome shown.
    await waitFor(() => expect(screen.getByRole('status')).toBeDefined())
    expect(screen.getByRole('checkbox')).toBeDefined()

    // Next session: the stale outcome must not survive — it would replace the
    // Yes/No buttons with a dead "… updated." line.
    await act(async () => {
      window.dispatchEvent(
        new CustomEvent('rootcoz:review-changed', { detail: { jobId: 'job-1' } }),
      )
    })
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Yes' })).toBeDefined()
      expect(screen.queryByRole('status')).toBeNull()
    })
  })
})

describe('AllReviewedPrompt greenwave section', () => {
  const GREENWAVE_ONLY = [{ name: 'greenwave', display_name: 'Greenwave', enabled: true }]

  function renderWithExporters(exporters: { name: string; display_name: string; enabled: boolean }[]) {
    function Seed() {
      const dispatch = useReportDispatch()
      useEffect(() => {
        dispatch({ type: 'SET_EXPORTERS', payload: exporters })
        dispatch({ type: 'SET_GREENWAVE_WAIVERS_ENABLED', payload: false })
      }, []) // eslint-disable-line react-hooks/exhaustive-deps
      return null
    }
    return render(
      <ReportProvider>
        <Seed />
        <Injector
          jobId="job-1"
          result={makeResult()}
          reviews={{}}
          reportportalAvailable={false}
          additionalReviews={{
            'test-a': makeReview(true),
            'test-b': makeReview(true),
          }}
        />
      </ReportProvider>,
    )
  }

  it('offers the greenwave form when the exporter is enabled', async () => {
    renderWithExporters(GREENWAVE_ONLY)
    await waitFor(() => {
      expect(screen.getByText('Push the reviewed failures to Greenwave')).toBeDefined()
      expect(screen.getByRole('checkbox')).toBeDefined()
    })
    // No event exporter to answer — the Close action is the only exit.
    expect(screen.queryByRole('button', { name: 'Yes' })).toBeNull()
  })

  it('says so instead of offering a push when the root has no failures', async () => {
    function Seed() {
      const dispatch = useReportDispatch()
      useEffect(() => {
        dispatch({ type: 'SET_EXPORTERS', payload: GREENWAVE_ONLY })
      }, []) // eslint-disable-line react-hooks/exhaustive-deps
      return null
    }
    const childOnly = makeResult({ failures: [] })
    render(
      <ReportProvider>
        <Seed />
        <Injector
          jobId="job-1"
          result={childOnly}
          reviews={{}}
          reportportalAvailable={false}
          additionalReviews={{}}
        />
      </ReportProvider>,
    )
    // Nothing to offer: no approval checkbox, no push form.
    expect(screen.queryByRole('checkbox')).toBeNull()
  })
})

describe('AllReviewedPrompt greenwave session scoping', () => {
  // Both exporters enabled: a successful Greenwave push leaves the dialog OPEN
  // (the event question is unanswered), which is the only state in which the
  // completed flag can survive into the next session. With Greenwave alone the
  // push closes the dialog and the flag is cleared by that path instead — a
  // test in that shape passes without the session guard and proves nothing.
  const BOTH_EXPORTERS = [
    { name: 'reportportal', display_name: 'Report Portal', enabled: true },
    { name: 'greenwave', display_name: 'Greenwave', enabled: true },
  ]

  function Seed() {
    const dispatch = useReportDispatch()
    useEffect(() => {
      dispatch({ type: 'SET_EXPORTERS', payload: BOTH_EXPORTERS })
      dispatch({ type: 'SET_GREENWAVE_WAIVERS_ENABLED', payload: false })
    }, []) // eslint-disable-line react-hooks/exhaustive-deps
    return null
  }

  function prompt(jobId: string) {
    return (
      <ReportProvider>
        <Seed />
        <Injector
          jobId={jobId}
          result={makeResult()}
          reviews={{}}
          reportportalAvailable={true}
          additionalReviews={{
            'test-a': makeReview(true),
            'test-b': makeReview(true),
          }}
        />
      </ReportProvider>
    )
  }

  /** Approve the text, supply an identifier, and push. */
  function pushToGreenwave() {
    fireEvent.change(screen.getByLabelText(/subject identifier/i), {
      target: { value: 'my-nvr-1.0' },
    })
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: /push to greenwave/i }))
  }

  it('ignores a push that lands after navigating to another report', async () => {
    let settle: (value: unknown) => void = () => {}
    mockPost.mockReturnValueOnce(
      new Promise((resolve) => {
        settle = resolve
      }) as never,
    )

    const { rerender } = render(prompt('job-1'))
    await waitFor(() => expect(screen.getByRole('checkbox')).toBeDefined())
    pushToGreenwave()

    // Navigate while the push is still in flight. The next report's prompt
    // opens with its own form; the old job's completion must not mark that
    // form as already pushed.
    rerender(prompt('job-2'))
    await act(async () => {
      window.dispatchEvent(
        new CustomEvent('rootcoz:review-changed', { detail: { jobId: 'job-2' } }),
      )
    })
    await waitFor(() => expect(screen.getByRole('checkbox')).toBeDefined())

    await act(async () => {
      settle({ success: true, message: 'pushed', pushed: 2, waived: 2, skipped: 0 })
    })

    expect(screen.getByRole('checkbox')).toBeDefined()
    expect(screen.queryByText(/Greenwave push completed/)).toBeNull()
  })

  it('ignores a push that lands after the prompt was closed and reopened', async () => {
    let settle: (value: unknown) => void = () => {}
    mockPost.mockReturnValueOnce(
      new Promise((resolve) => {
        settle = resolve
      }) as never,
    )

    render(prompt('job-1'))
    await waitFor(() => expect(screen.getByRole('checkbox')).toBeDefined())
    pushToGreenwave()

    // Dismiss the prompt while the push is in flight, then start a new
    // session: the in-flight completion belongs to the dead one.
    fireEvent.keyDown(document, { key: 'Escape' })
    await act(async () => {
      window.dispatchEvent(
        new CustomEvent('rootcoz:review-changed', { detail: { jobId: 'job-1' } }),
      )
    })
    await waitFor(() => expect(screen.getByRole('checkbox')).toBeDefined())

    await act(async () => {
      settle({ success: true, message: 'pushed', pushed: 2, waived: 2, skipped: 0 })
    })

    // The live session's form must survive: it was never pushed.
    expect(screen.getByRole('checkbox')).toBeDefined()
    expect(screen.queryByText(/Greenwave push completed/)).toBeNull()
  })
})

describe('AllReviewedPrompt keeps the greenwave offer after an event push', () => {
  it('stays open with the form visible when Report Portal succeeds', async () => {
    const BOTH = [
      { name: 'reportportal', display_name: 'Report Portal', enabled: true },
      { name: 'greenwave', display_name: 'Greenwave', enabled: true },
    ]
    function Seed() {
      const dispatch = useReportDispatch()
      useEffect(() => {
        dispatch({ type: 'SET_EXPORTERS', payload: BOTH })
        dispatch({ type: 'SET_GREENWAVE_WAIVERS_ENABLED', payload: false })
      }, []) // eslint-disable-line react-hooks/exhaustive-deps
      return null
    }
    render(
      <ReportProvider>
        <Seed />
        <Injector
          jobId="job-1"
          result={makeResult()}
          reviews={{}}
          reportportalAvailable={true}
          additionalReviews={{
            'test-a': makeReview(true),
            'test-b': makeReview(true),
          }}
        />
      </ReportProvider>,
    )

    await waitFor(() => expect(screen.getByRole('button', { name: 'Yes' })).toBeDefined())
    mockPost.mockResolvedValueOnce({ success: true, message: 'ok', pushed: 2 })
    fireEvent.click(screen.getByRole('button', { name: 'Yes' }))

    await waitFor(() => {
      expect(screen.getByRole('status')).toBeDefined()
      // The Greenwave offer survives the Report Portal answer.
      expect(screen.getByRole('checkbox')).toBeDefined()
      expect(screen.getByText('Push the reviewed failures to Greenwave')).toBeDefined()
    })
  })
})
