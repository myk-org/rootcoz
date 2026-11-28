import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect } from 'react'
import { api, ApiError } from '@/lib/api'
import { GreenwaveButton } from '../GreenwaveButton'
import { useReportDispatch } from '@/pages/report/reportState'
import { ReportProvider } from '../ReportContext'
import type { AnalysisResult, ExporterInfo, FailureAnalysis, ReviewState } from '@/types'

/* ------------------------------------------------------------------ */
/*  Mocks / helpers                                                    */
/* ------------------------------------------------------------------ */

const postSpy = vi.spyOn(api, 'post')

const EXPORTERS: ExporterInfo[] = [
  { name: 'reportportal', display_name: 'Report Portal', enabled: true },
  { name: 'greenwave', display_name: 'Greenwave', enabled: true },
]

function makeFailure(classification: string, testName: string): FailureAnalysis {
  return {
    id: `f-${testName}`,
    test_name: testName,
    error: 'err',
    analysis: {
      classification,
      pattern: '',
      affected_tests: [testName],
      details: '',
      artifacts_evidence: '',
    },
    error_signature: `sig-${testName}`,
  } as FailureAnalysis
}

const FAILURES = [makeFailure('PRODUCT BUG', 'test-a'), makeFailure('CODE ISSUE', 'test-b')]

const REVIEWED: ReviewState = { reviewed: true, username: 'user', updated_at: '2025-01-01T00:00:00Z' }
const UNREVIEWED: ReviewState = { ...REVIEWED, reviewed: false }

function Injector({ exporters, greenwaveWaivers, failures, reviews }: {
  exporters: ExporterInfo[]
  greenwaveWaivers: boolean
  failures?: FailureAnalysis[]
  reviews?: Record<string, ReviewState>
}) {
  const dispatch = useReportDispatch()
  useEffect(() => {
    dispatch({ type: 'SET_EXPORTERS', payload: exporters })
    dispatch({ type: 'SET_GREENWAVE_WAIVERS_ENABLED', payload: greenwaveWaivers })
    if (reviews) {
      // The gate counts reviews over the report result, so it needs both.
      dispatch({
        type: 'SET_RESULT',
        payload: {
          result: { job_id: 'job-1', job_name: 'my-job', build_number: 42, failures: failures ?? FAILURES } as AnalysisResult,
          createdAt: '',
          completedAt: '',
          analysisStartedAt: '',
        },
      })
      dispatch({ type: 'SET_COMMENTS_AND_REVIEWS', payload: { comments: [], reviews } })
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <GreenwaveButton jobId="job-1" jobName="my-job" buildNumber={42} failures={failures ?? FAILURES} />
  )
}

function renderButton(opts: {
  exporters?: ExporterInfo[]
  greenwaveWaivers?: boolean
  reviews?: Record<string, ReviewState>
} = {}) {
  return render(
    <ReportProvider>
      <Injector
        exporters={opts.exporters ?? EXPORTERS}
        greenwaveWaivers={opts.greenwaveWaivers ?? false}
        reviews={opts.reviews}
      />
    </ReportProvider>,
  )
}

async function openConfirmDialog() {
  fireEvent.click(screen.getByRole('button', { name: 'Push to Greenwave' }))
  await waitFor(() => {
    expect(screen.getByLabelText(/waiver comment/i)).toBeDefined()
  })
}

/** Two buttons carry the label while the dialog is open (the page-level ghost
 *  button behind the overlay and the form's submit) — the form's is last. */
function formPushButton(): HTMLButtonElement {
  return screen.getAllByRole('button', { name: 'Push to Greenwave' }).at(-1) as HTMLButtonElement
}

function typeIdentifier(value = 'my-nvr-1.0') {
  fireEvent.change(screen.getByLabelText(/subject identifier/i), { target: { value } })
}

/** Identifier + approval: an identifier is mandatory for every push now. */
function readyToPush(value?: string) {
  typeIdentifier(value)
  fireEvent.click(screen.getByRole('checkbox'))
}

function approve() {
  fireEvent.click(screen.getByRole('checkbox'))
}

beforeEach(() => {
  postSpy.mockReset()
  postSpy.mockResolvedValue({ success: true, message: 'pushed', pushed: 2, waived: 1, skipped: 0 } as never)
})

describe('GreenwaveButton', () => {
  it('is not rendered when the greenwave exporter is not enabled', () => {
    renderButton({
      exporters: [{ name: 'reportportal', display_name: 'Report Portal', enabled: true }],
    })
    expect(screen.queryByRole('button', { name: /push to greenwave/i })).toBeNull()
  })

  it('is not rendered when the exporter is present but disabled', () => {
    renderButton({
      exporters: [{ name: 'greenwave', display_name: 'Greenwave', enabled: false }],
    })
    expect(screen.queryByRole('button', { name: /push to greenwave/i })).toBeNull()
  })

  it('is rendered when the greenwave exporter is enabled', () => {
    renderButton()
    expect(screen.getByRole('button', { name: /push to greenwave/i })).toBeDefined()
  })

  it('is disabled with a hint when there are no failures to push', () => {
    render(
      <ReportProvider>
        <Injector exporters={EXPORTERS} greenwaveWaivers={false} failures={[]} />
      </ReportProvider>,
    )
    expect(screen.getByRole('button', { name: 'Push to Greenwave' })).toHaveProperty('disabled', true)
  })

  it('is enabled and pushes when every failure is reviewed', () => {
    renderButton({ reviews: { 'test-a': REVIEWED, 'test-b': REVIEWED } })
    expect(screen.getByRole('button', { name: 'Push to Greenwave' })).toHaveProperty('disabled', false)
  })

  it('disables the button and names the unreviewed gate — with no escape hatch to offer', async () => {
    renderButton({ reviews: { 'test-a': REVIEWED, 'test-b': UNREVIEWED } })

    const button = screen.getByRole('button', { name: 'Push to Greenwave' })
    expect(button).toHaveProperty('disabled', true)

    // Radix hangs the trigger on the wrapper span, as with the other exporters.
    await userEvent.hover(button.closest('span')!)
    const hint = await screen.findByTestId('greenwave-push-gate-hint')
    expect(hint.textContent).toBe('1 failure(s) not yet reviewed — review them before pushing')
    // Greenwave has no ALLOW_UNREVIEWED_PUSH setting to point admins at.
    expect(hint.textContent).not.toMatch(/admin|enable/i)
    expect(postSpy).not.toHaveBeenCalled()
  })

  it('blocks submit and shows an inline reason when waivers are on and the identifier is blank', async () => {
    renderButton({ greenwaveWaivers: true })
    await openConfirmDialog()
    approve()

    expect(formPushButton()).toHaveProperty('disabled', true)
    expect(
      screen.getByText(
        'Subject identifier is required: ResultsDB and WaiverDB are both keyed on it.',
      ),
    ).toBeDefined()
    expect(postSpy).not.toHaveBeenCalled()
  })

  it('posts subject_identifier and waiver_comment when waivers are on and the identifier is filled', async () => {
    renderButton({ greenwaveWaivers: true })
    await openConfirmDialog()

    fireEvent.change(screen.getByLabelText(/subject identifier/i), { target: { value: 'my-package-1.0' } })
    fireEvent.change(screen.getByLabelText(/waiver comment/i), { target: { value: 'known flake' } })
    approve()
    fireEvent.click(formPushButton())

    await waitFor(() => {
      expect(postSpy).toHaveBeenCalledWith(
        '/results/job-1/push/greenwave?subject_identifier=my-package-1.0&waiver_comment=known+flake',
      )
    })
  })

  it('requires the identifier in results-only mode too', async () => {
    renderButton({ greenwaveWaivers: false })
    await openConfirmDialog()
    readyToPush()
    fireEvent.click(formPushButton())

    await waitFor(() => {
      // The identifier is sent even without waivers: ResultsDB is keyed on it.
      expect(postSpy).toHaveBeenCalledWith(
        expect.stringContaining('subject_identifier=my-nvr-1.0'),
      )
    })
  })

  it('renders success counts on a successful push', async () => {
    renderButton()
    await openConfirmDialog()
    readyToPush()
    fireEvent.click(formPushButton())

    await waitFor(() => {
      expect(screen.getByText(/Pushed 2 results to Greenwave/)).toBeDefined()
    })
    expect(screen.getByText('Pushed: 2')).toBeDefined()
    expect(screen.getByText('Waived: 1')).toBeDefined()
    expect(screen.getByText('Skipped: 0')).toBeDefined()
  })

  it('renders a no-op amber result when nothing matched the outcome map', async () => {
    // success=false with pushed=0 and no errors means "nothing to push", not a failure
    postSpy.mockResolvedValue({ success: false, message: 'no matching outcome', pushed: 0, errors: [] } as never)
    renderButton()
    await openConfirmDialog()
    readyToPush()
    fireEvent.click(formPushButton())

    await waitFor(() => {
      expect(screen.getByText(/No results could be pushed — no failure matched GREENWAVE_OUTCOME_MAP/)).toBeDefined()
    })
    expect(screen.queryByText(/Failed to push to Greenwave/)).toBeNull()
  })

  it('renders the server detail message on a 422 error', async () => {
    postSpy.mockRejectedValueOnce(
      new ApiError(422, 'Unprocessable Entity', { detail: 'subject_identifier is required for waivers' }),
    )
    renderButton({ greenwaveWaivers: false })
    await openConfirmDialog()
    readyToPush()
    fireEvent.click(formPushButton())

    await waitFor(() => {
      expect(screen.getByText(/Failed to push to Greenwave/)).toBeDefined()
    })
    expect(screen.getByText('subject_identifier is required for waivers')).toBeDefined()
  })

  it('lists result errors as a partial failure', async () => {
    postSpy.mockResolvedValue({
      success: true, message: 'partial', pushed: 1, errors: ['could not match test-b'],
    } as never)
    renderButton()
    await openConfirmDialog()
    readyToPush()
    fireEvent.click(formPushButton())

    await waitFor(() => {
      expect(screen.getByText(/Some results could not be pushed/)).toBeDefined()
    })
    expect(screen.getByText('could not match test-b')).toBeDefined()
  })
})
