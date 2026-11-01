import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { api, ApiError } from '@/lib/api'
import { GreenwavePushForm } from '../GreenwavePushForm'
import { useReportDispatch } from '@/pages/report/reportState'
import { ReportProvider } from '../ReportContext'
import { proposeWaiverComment } from '@/lib/greenwaveComment'
import type { ExporterInfo, FailureAnalysis } from '@/types'

/* ------------------------------------------------------------------ */
/*  Mocks / helpers                                                    */
/* ------------------------------------------------------------------ */

const postSpy = vi.spyOn(api, 'post')

const EXPORTERS: ExporterInfo[] = [
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

const FAILURES = [
  makeFailure('PRODUCT BUG', 'test-a'),
  makeFailure('PRODUCT BUG', 'test-b'),
  makeFailure('CODE ISSUE', 'test-c'),
]

function Injector({ greenwaveWaivers }: { greenwaveWaivers: boolean }) {
  const dispatch = useReportDispatch()
  useEffect(() => {
    dispatch({ type: 'SET_EXPORTERS', payload: EXPORTERS })
    dispatch({ type: 'SET_GREENWAVE_WAIVERS_ENABLED', payload: greenwaveWaivers })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return null
}

function renderForm(opts: { greenwaveWaivers?: boolean; onPushed?: () => void } = {}) {
  return render(
    <ReportProvider>
      <Injector greenwaveWaivers={opts.greenwaveWaivers ?? false} />
      <GreenwavePushForm
        jobId="job-1"
        jobName="my-job"
        buildNumber={42}
        failures={FAILURES}
        onPushed={opts.onPushed}
      />
    </ReportProvider>,
  )
}

function approve() {
  fireEvent.click(screen.getByRole('checkbox'))
}

beforeEach(() => {
  postSpy.mockReset()
  postSpy.mockResolvedValue({ success: true, message: 'pushed', pushed: 3, waived: 2, skipped: 1 } as never)
})

describe('GreenwavePushForm', () => {
  it('seeds the waiver comment with the deterministic proposal', () => {
    renderForm()
    expect(screen.getByLabelText(/waiver comment/i)).toHaveValue(
      proposeWaiverComment({ jobName: 'my-job', buildNumber: 42, failures: FAILURES }),
    )
  })

  it('blocks the push without approval', () => {
    renderForm()
    const pushButton = screen.getByRole('button', { name: /push to greenwave/i })
    expect(pushButton).toHaveProperty('disabled', true)
    expect(screen.getByText('Approve the text above to push.')).toBeDefined()
    expect(postSpy).not.toHaveBeenCalled()
  })

  it('blocks the push without a subject identifier when waivers are on', () => {
    renderForm({ greenwaveWaivers: true })
    approve()
    const pushButton = screen.getByRole('button', { name: /push to greenwave/i })
    expect(pushButton).toHaveProperty('disabled', true)
    expect(screen.getByText('Subject identifier is required when waivers are enabled.')).toBeDefined()
    expect(postSpy).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText(/subject identifier/i), { target: { value: 'my-nvr-1.0' } })
    expect(screen.getByRole('button', { name: /push to greenwave/i })).toHaveProperty('disabled', false)
  })

  it('sends subject_identifier and the trimmed comment when approved', async () => {
    renderForm({ greenwaveWaivers: true })
    fireEvent.change(screen.getByLabelText(/subject identifier/i), { target: { value: ' my-nvr-1.0 ' } })
    fireEvent.change(screen.getByLabelText(/waiver comment/i), { target: { value: '  rootcoz: reviewed  ' } })
    approve()
    fireEvent.click(screen.getByRole('button', { name: /push to greenwave/i }))

    await waitFor(() => {
      expect(postSpy).toHaveBeenCalledWith(
        '/results/job-1/push/greenwave?subject_identifier=my-nvr-1.0&waiver_comment=rootcoz%3A+reviewed',
      )
    })
  })

  it('voids the approval when the comment is edited', () => {
    renderForm()
    approve()
    expect(screen.getByRole('button', { name: /push to greenwave/i })).toHaveProperty('disabled', false)
    fireEvent.change(screen.getByLabelText(/waiver comment/i), { target: { value: 'edited' } })
    expect(screen.getByRole('button', { name: /push to greenwave/i })).toHaveProperty('disabled', true)
  })

  it('shows the final text in the read-only payload preview', () => {
    renderForm({ greenwaveWaivers: true })
    fireEvent.change(screen.getByLabelText(/subject identifier/i), { target: { value: 'my-nvr-1.0' } })
    fireEvent.change(screen.getByLabelText(/waiver comment/i), { target: { value: 'line one\n  line   two' } })

    expect(screen.getByText('Subject identifier: my-nvr-1.0')).toBeDefined()
    expect(screen.getByText('Waivers: on')).toBeDefined()
    expect(screen.getByText('Results: 3 failure(s)')).toBeDefined()
    expect(screen.getByText('Text: line one line two')).toBeDefined()
  })

  it('shows waivers off and the job-name fallback in results-only mode', () => {
    renderForm()
    expect(screen.getByText('Subject identifier: (job name — results only)')).toBeDefined()
    expect(screen.getByText('Waivers: off')).toBeDefined()
  })

  it('surfaces the server 422 detail on failure', async () => {
    postSpy.mockRejectedValueOnce(
      new ApiError(422, 'Unprocessable Entity', { detail: 'subject_identifier is required for waivers' }),
    )
    renderForm()
    approve()
    fireEvent.click(screen.getByRole('button', { name: /push to greenwave/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent('subject_identifier is required for waivers')
    expect(screen.getByText(/Failed to push to Greenwave/)).toBeDefined()
  })

  it('calls onPushed after a successful roundtrip', async () => {
    const onPushed = vi.fn()
    renderForm({ onPushed })
    approve()
    fireEvent.click(screen.getByRole('button', { name: /push to greenwave/i }))

    await waitFor(() => expect(onPushed).toHaveBeenCalledOnce())
  })
})
