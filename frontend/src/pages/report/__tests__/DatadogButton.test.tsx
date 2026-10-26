import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect, type ReactNode } from 'react'
import { DatadogButton } from '@/pages/report/DatadogButton'
import { ReportProvider } from '@/pages/report/ReportContext'
import { useReportDispatch } from '@/pages/report/reportState'
import { ApiError } from '@/lib/api'
import type { AnalysisResult, DatadogPushResult, ReviewState } from '@/types'

const mockPost = vi.fn()

// Keep the real module (ApiError, extractApiDetail) so the 409 gate-rejection
// detection is exercised exactly as in production; only api.post is mocked.
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
        analysis: { classification: 'PRODUCT BUG', pattern: '', affected_tests: ['test-a'], details: '', artifacts_evidence: '' },
        error_signature: 'sig-a',
      },
      {
        id: 'f-b',
        test_name: 'test-b',
        error: 'error-b',
        analysis: { classification: 'CODE ISSUE', pattern: '', affected_tests: ['test-b'], details: '', artifacts_evidence: '' },
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

function makeResultBody(overrides: Partial<DatadogPushResult> = {}): DatadogPushResult {
  return {
    success: true,
    message: 'Pushed 2 Datadog event(s)',
    pushed: 2,
    total: 2,
    errors: [],
    skipped: 0,
    ...overrides,
  }
}

/** Injects report state (result, reviews, escape hatch) then renders children. */
function Injector({
  result,
  reviews,
  allowUnreviewedPush,
  children,
}: {
  result: AnalysisResult
  reviews: Record<string, ReviewState>
  allowUnreviewedPush: boolean
  children: ReactNode
}) {
  const dispatch = useReportDispatch()
  useEffect(() => {
    dispatch({ type: 'SET_RESULT', payload: { result, createdAt: '', completedAt: '', analysisStartedAt: '' } })
    dispatch({ type: 'SET_COMMENTS_AND_REVIEWS', payload: { comments: [], reviews } })
    dispatch({ type: 'SET_EXPORTER_ALLOW_UNREVIEWED', payload: { reportportal: allowUnreviewedPush, datadog: allowUnreviewedPush } })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return <>{children}</>
}

function renderButton(opts: {
  hasFailures?: boolean
  result?: AnalysisResult
  reviews?: Record<string, ReviewState>
  allowUnreviewedPush?: boolean
} = {}) {
  const result = opts.result ?? makeResult()
  return render(
    <ReportProvider>
      <Injector result={result} reviews={opts.reviews ?? {}} allowUnreviewedPush={opts.allowUnreviewedPush ?? false}>
        <DatadogButton
          jobId="job-1"
          jobName="test-job"
          buildNumber={42}
          hasFailures={opts.hasFailures ?? true}
        />
      </Injector>
    </ReportProvider>,
  )
}

/** Both failures reviewed — the default "clean" state for non-gate tests. */
function allReviewed(): Record<string, ReviewState> {
  return { 'test-a': makeReview(true), 'test-b': makeReview(true) }
}

/** Both failures unreviewed — the gate would block the push. */
function noneReviewed(): Record<string, ReviewState> {
  return { 'test-a': makeReview(false), 'test-b': makeReview(false) }
}

async function confirmPush() {
  await userEvent.click(screen.getByRole('button', { name: /push to datadog/i }))
  await waitFor(() => {
    expect(screen.getByRole('button', { name: /^push$/i })).toBeDefined()
  })
  await userEvent.click(screen.getByRole('button', { name: /^push$/i }))
}

describe('DatadogButton', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockPost.mockResolvedValue(makeResultBody())
  })

  it('renders the push button when failures exist', () => {
    renderButton({ reviews: allReviewed() })
    expect(screen.getByRole('button', { name: /push to datadog/i })).toBeDefined()
  })

  it('disables the button when there are no failures', () => {
    renderButton({ hasFailures: false, reviews: allReviewed() })
    expect(screen.getByRole('button', { name: /push to datadog/i })).toBeDisabled()
  })

  it('calls the datadog push endpoint on confirm', async () => {
    renderButton({ reviews: allReviewed() })
    await confirmPush()

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/results/job-1/push/datadog')
    })
  })

  it('shows a success result dialog with the pushed count', async () => {
    renderButton({ reviews: allReviewed() })
    await confirmPush()

    await waitFor(() => {
      expect(screen.getByText('Pushed 2 events to Datadog.')).toBeDefined()
    })
  })

  it('shows a partial-failure result dialog with errors and skipped counts', async () => {
    mockPost.mockResolvedValue(
      makeResultBody({
        success: false,
        pushed: 1,
        total: 3,
        errors: ['event 1 failed'],
        skipped: 1,
      }),
    )

    renderButton({ reviews: allReviewed() })
    await confirmPush()

    await waitFor(() => {
      expect(screen.getByText('Some events could not be pushed.')).toBeDefined()
    })
    expect(screen.getByText('event 1 failed')).toBeDefined()
    expect(screen.getByText('1 event skipped')).toBeDefined()
  })

  it('shows a failure dialog with the abort message when success=false and nothing was pushed', async () => {
    mockPost.mockResolvedValue(
      makeResultBody({
        success: false,
        pushed: 0,
        total: 0,
        errors: [],
        skipped: 0,
        message: 'Datadog API key rejected',
      }),
    )

    renderButton({ reviews: allReviewed() })
    await confirmPush()

    await waitFor(() => {
      expect(screen.getByText('Failed to push events to Datadog.')).toBeDefined()
    })
    expect(screen.getByText('Datadog API key rejected')).toBeDefined()
    // Must not render the green zero-count success message
    expect(screen.queryByText('Pushed 0 events to Datadog.')).toBeNull()
  })

  it('shows a full-failure result dialog when the request fails', async () => {
    mockPost.mockRejectedValue(new Error('Network error'))

    renderButton({ reviews: allReviewed() })
    await confirmPush()

    await waitFor(() => {
      expect(screen.getByText('Failed to push events to Datadog.')).toBeDefined()
    })
  })

  it('disables the button with an explanatory tooltip when failures are unreviewed and the hatch is off', async () => {
    renderButton({ reviews: noneReviewed(), allowUnreviewedPush: false })

    const button = screen.getByRole('button', { name: /push to datadog/i })
    expect(button).toBeDisabled()

    // Radix hangs the trigger on the wrapper span — hovering the disabled
    // button itself would swallow the pointer events.
    await userEvent.hover(button.closest('span')!)
    expect(
      await screen.findByText(
        '2 failure(s) not yet reviewed — review them before pushing (or ask an admin to enable DD_ALLOW_UNREVIEWED_PUSH)',
      ),
    ).toBeDefined()
  })

  it('keeps the button enabled when the escape hatch is on', () => {
    renderButton({ reviews: noneReviewed(), allowUnreviewedPush: true })

    expect(screen.getByRole('button', { name: /push to datadog/i })).toBeEnabled()
  })

  it('keeps the button enabled when all failures are reviewed', () => {
    renderButton({ reviews: allReviewed(), allowUnreviewedPush: false })

    expect(screen.getByRole('button', { name: /push to datadog/i })).toBeEnabled()
  })

  it('renders an actionable gate message on a 409 unreviewed-failure rejection, distinct from generic failures', async () => {
    mockPost.mockRejectedValue(
      new ApiError(409, 'Conflict', { detail: 'job has 2 unreviewed failure(s) — review them before pushing' }),
    )

    renderButton({ reviews: noneReviewed(), allowUnreviewedPush: true })
    await confirmPush()

    const gateMessage = await screen.findByTestId('datadog-push-gate-message')
    expect(gateMessage.textContent).toContain('2 unreviewed failure(s)')
    expect(gateMessage.textContent).toContain('DD_ALLOW_UNREVIEWED_PUSH')
    // Distinct from the generic red failure banner
    expect(screen.getByText('Push blocked — unreviewed failures.')).toBeDefined()
    expect(screen.queryByText('Failed to push events to Datadog.')).toBeNull()
  })

  it('renders the generic failure banner for a 403 auth rejection, not the gate message', async () => {
    mockPost.mockRejectedValue(
      new ApiError(403, 'Forbidden', { detail: 'Datadog API key rejected' }),
    )

    renderButton({ reviews: allReviewed() })
    await confirmPush()

    await waitFor(() => {
      expect(screen.getByText('Failed to push events to Datadog.')).toBeDefined()
    })
    expect(screen.queryByTestId('datadog-push-gate-message')).toBeNull()
    expect(screen.getByText('Datadog API key rejected')).toBeDefined()
  })
})
