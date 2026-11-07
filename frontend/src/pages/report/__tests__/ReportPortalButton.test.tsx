import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect, type ReactNode } from 'react'
import { ReportPortalButton } from '@/pages/report/ReportPortalButton'
import { ReportProvider } from '@/pages/report/ReportContext'
import { useReportDispatch } from '@/pages/report/reportState'
import { ApiError } from '@/lib/api'
import type { AnalysisResult, ReportPortalPushResult, ReviewState } from '@/types'

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

function makeResult(): AnalysisResult {
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
  }
}

function makeReview(reviewed: boolean): ReviewState {
  return { reviewed, username: 'user', updated_at: '2025-01-01T00:00:00Z' }
}

function makePushBody(overrides: Partial<ReportPortalPushResult> = {}): ReportPortalPushResult {
  return { pushed: 2, unmatched: [], errors: [], launch_id: null, ...overrides }
}

/** Both failures reviewed — the default "clean" state for non-gate tests. */
function allReviewed(): Record<string, ReviewState> {
  return { 'test-a': makeReview(true), 'test-b': makeReview(true) }
}

/** Both failures unreviewed — the gate would block the push. */
function noneReviewed(): Record<string, ReviewState> {
  return { 'test-a': makeReview(false), 'test-b': makeReview(false) }
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
  reviews: Record<string, ReviewState>
  allowUnreviewedPush: boolean
}) {
  return render(
    <ReportProvider>
      <Injector result={makeResult()} reviews={opts.reviews} allowUnreviewedPush={opts.allowUnreviewedPush}>
        <ReportPortalButton jobId="job-1" jobName="test-job" buildNumber={42} hasFailures />
      </Injector>
    </ReportProvider>,
  )
}

async function confirmPush() {
  await userEvent.click(screen.getByRole('button', { name: /push to report portal/i }))
  await waitFor(() => {
    expect(screen.getByRole('button', { name: /^push$/i })).toBeDefined()
  })
  await userEvent.click(screen.getByRole('button', { name: /^push$/i }))
}

describe('ReportPortalButton', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockPost.mockResolvedValue(makePushBody())
  })

  it('pushes to the reportportal endpoint on confirm', async () => {
    renderButton({ reviews: allReviewed(), allowUnreviewedPush: false })
    await confirmPush()

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith('/results/job-1/push-reportportal')
    })
    expect(await screen.findByText('Pushed 2 classifications to Report Portal.')).toBeDefined()
  })

  it('disables the button with an explanatory tooltip when failures are unreviewed and the hatch is off', async () => {
    renderButton({ reviews: noneReviewed(), allowUnreviewedPush: false })

    const button = screen.getByRole('button', { name: /push to report portal/i })
    expect(button).toBeDisabled()

    // Radix hangs the trigger on the wrapper span — hovering the disabled
    // button itself would swallow the pointer events.
    await userEvent.hover(button.closest('span')!)
    expect(
      await screen.findByText(
        '2 failure(s) not yet reviewed — review them before pushing (or ask an admin to enable RP_ALLOW_UNREVIEWED_PUSH)',
      ),
    ).toBeDefined()
  })

  it('keeps the button enabled when the escape hatch is on', () => {
    renderButton({ reviews: noneReviewed(), allowUnreviewedPush: true })

    expect(screen.getByRole('button', { name: /push to report portal/i })).toBeEnabled()
  })

  it('keeps the button enabled when all failures are reviewed', () => {
    renderButton({ reviews: allReviewed(), allowUnreviewedPush: false })

    expect(screen.getByRole('button', { name: /push to report portal/i })).toBeEnabled()
  })

  it('renders an actionable gate message on a 409 unreviewed-failure rejection, distinct from generic failures', async () => {
    mockPost.mockRejectedValue(
      new ApiError(409, 'Conflict', { detail: 'job has 2 unreviewed failure(s) — review them before pushing' }),
    )

    renderButton({ reviews: noneReviewed(), allowUnreviewedPush: true })
    await confirmPush()

    const gateMessage = await screen.findByTestId('reportportal-push-gate-message')
    expect(gateMessage.textContent).toContain('2 unreviewed failure(s)')
    expect(gateMessage.textContent).toContain('RP_ALLOW_UNREVIEWED_PUSH')
    // Distinct from the generic red failure banner
    expect(screen.getByText('Push blocked — unreviewed failures.')).toBeDefined()
    expect(screen.queryByText('Failed to push classifications to Report Portal.')).toBeNull()
  })

  it('renders the generic failure banner for a non-gate rejection, not the gate message', async () => {
    mockPost.mockRejectedValue(new Error('Network error'))

    renderButton({ reviews: allReviewed(), allowUnreviewedPush: false })
    await confirmPush()

    await waitFor(() => {
      expect(screen.getByText('Failed to push classifications to Report Portal.')).toBeDefined()
    })
    expect(screen.queryByTestId('reportportal-push-gate-message')).toBeNull()
  })

  it('explains unmatched tests as a missing classification, not an RP matching failure', async () => {
    mockPost.mockResolvedValue(
      makePushBody({
        pushed: 0,
        unmatched: ['test-a (classification: UNKNOWN)', 'test-b (classification: UNKNOWN)'],
      }),
    )

    renderButton({ reviews: allReviewed(), allowUnreviewedPush: false })
    await confirmPush()

    const reason = await screen.findByTestId('reportportal-push-unmatched')
    expect(reason.textContent).toContain(
      '2 test(s) have no classification to map to a Report Portal defect type',
    )
    // Nothing was pushed, so there is no split to state
    expect(reason.textContent).not.toContain('Pushed 0')
    expect(reason.textContent).not.toContain('could not be matched')
    expect(screen.queryByText(/could not be matched/i)).toBeNull()
    // The entries name the offending classification, on their own line
    expect(screen.getByTestId('reportportal-push-unmatched-items').textContent).toContain(
      'classification: UNKNOWN',
    )
  })

  it('shows the pushed/skipped split for a partial push', async () => {
    mockPost.mockResolvedValue(
      makePushBody({ pushed: 3, unmatched: ['test-a (classification: UNKNOWN)'] }),
    )

    renderButton({ reviews: allReviewed(), allowUnreviewedPush: false })
    await confirmPush()

    const reason = await screen.findByTestId('reportportal-push-unmatched')
    expect(reason.textContent).toContain('Pushed 3 · skipped 1')
    expect(reason.textContent).toContain(
      '1 test(s) have no classification to map to a Report Portal defect type',
    )
  })

  it('treats TO INVESTIGATE-defaulted tests as pushed, not skipped', async () => {
    mockPost.mockResolvedValue(
      makePushBody({ pushed: 3, defaulted: ['test-a (classification: none)'], unmatched: [] }),
    )

    renderButton({ reviews: allReviewed(), allowUnreviewedPush: false })
    await confirmPush()

    expect(await screen.findByText('Pushed 3 classifications to Report Portal.')).toBeDefined()
    expect(screen.getByTestId('reportportal-push-defaulted').textContent).toContain(
      '1 test(s) were pushed as TO INVESTIGATE because they have no mappable classification',
    )
    expect(screen.getByTestId('reportportal-push-defaulted-items').textContent).toContain(
      'test-a (classification: none)',
    )
    // Information, not a warning: success banner, no skipped run, no failure copy
    expect(screen.queryByText('Some classifications could not be pushed.')).toBeNull()
    expect(screen.queryByText('No classifications could be pushed.')).toBeNull()
    expect(screen.queryByTestId('reportportal-push-unmatched')).toBeNull()
    expect(screen.queryByText(/skipped/)).toBeNull()
  })

  it('counts only unmatched tests as skipped when defaulted tests were pushed', async () => {
    mockPost.mockResolvedValue(
      makePushBody({
        pushed: 3,
        defaulted: ['test-a (classification: none)'],
        unmatched: ['test-b (classification: UNKNOWN)'],
      }),
    )

    renderButton({ reviews: allReviewed(), allowUnreviewedPush: false })
    await confirmPush()

    const reason = await screen.findByTestId('reportportal-push-unmatched')
    expect(reason.textContent).toContain('Pushed 3 · skipped 1')
    expect(screen.getByText('Some classifications could not be pushed.')).toBeDefined()
    expect(screen.getByTestId('reportportal-push-defaulted')).toBeDefined()
  })
})
