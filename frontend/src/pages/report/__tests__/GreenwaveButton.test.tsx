import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { api, ApiError } from '@/lib/api'
import { GreenwaveButton } from '../GreenwaveButton'
import { useReportDispatch } from '@/pages/report/reportState'
import { ReportProvider } from '../ReportContext'
import type { ExporterInfo, FailureAnalysis } from '@/types'

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

function Injector({ exporters, greenwaveWaivers, failures }: {
  exporters: ExporterInfo[]
  greenwaveWaivers: boolean
  failures?: FailureAnalysis[]
}) {
  const dispatch = useReportDispatch()
  useEffect(() => {
    dispatch({ type: 'SET_EXPORTERS', payload: exporters })
    dispatch({ type: 'SET_GREENWAVE_WAIVERS_ENABLED', payload: greenwaveWaivers })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <GreenwaveButton jobId="job-1" jobName="my-job" buildNumber={42} failures={failures ?? FAILURES} />
  )
}

function renderButton(opts: { exporters?: ExporterInfo[]; greenwaveWaivers?: boolean } = {}) {
  return render(
    <ReportProvider>
      <Injector
        exporters={opts.exporters ?? EXPORTERS}
        greenwaveWaivers={opts.greenwaveWaivers ?? false}
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

  it('blocks submit and shows an inline reason when waivers are on and the identifier is blank', async () => {
    renderButton({ greenwaveWaivers: true })
    await openConfirmDialog()
    approve()

    expect(formPushButton()).toHaveProperty('disabled', true)
    expect(screen.getByText('Subject identifier is required when waivers are enabled.')).toBeDefined()
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

  it('does not require the identifier in results-only mode', async () => {
    renderButton({ greenwaveWaivers: false })
    await openConfirmDialog()
    approve()
    fireEvent.click(formPushButton())

    await waitFor(() => {
      // Results-only mode sends no subject_identifier; the seeded comment is sent
      expect(postSpy).toHaveBeenCalledWith(
        expect.stringContaining('/results/job-1/push/greenwave?waiver_comment=rootcoz%3A'),
      )
    })
  })

  it('renders success counts on a successful push', async () => {
    renderButton()
    await openConfirmDialog()
    approve()
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
    approve()
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
    approve()
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
    approve()
    fireEvent.click(formPushButton())

    await waitFor(() => {
      expect(screen.getByText(/Some results could not be pushed/)).toBeDefined()
    })
    expect(screen.getByText('could not match test-b')).toBeDefined()
  })
})
