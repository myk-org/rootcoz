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

function typeIdentifier(value = 'my-nvr-1.0') {
  fireEvent.change(screen.getByLabelText(/subject identifier/i), { target: { value } })
}

function approve() {
  fireEvent.click(screen.getByRole('checkbox'))
}

/** Identifier + approval: the minimum for a pushable payload. */
function readyToPush(value?: string) {
  typeIdentifier(value)
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

  it('requires the subject identifier for results-only pushes too', () => {
    renderForm()
    approve()
    const pushButton = screen.getByRole('button', { name: /push to greenwave/i })
    expect(pushButton).toHaveProperty('disabled', true)
    expect(
      screen.getByText(
        'Subject identifier is required: ResultsDB and WaiverDB are both keyed on it.',
      ),
    ).toBeDefined()
    expect(postSpy).not.toHaveBeenCalled()
  })

  it('blocks the push without a subject identifier when waivers are on', () => {
    renderForm({ greenwaveWaivers: true })
    approve()
    const pushButton = screen.getByRole('button', { name: /push to greenwave/i })
    expect(pushButton).toHaveProperty('disabled', true)
    expect(
      screen.getByText(
        'Subject identifier is required: ResultsDB and WaiverDB are both keyed on it.',
      ),
    ).toBeDefined()
    expect(postSpy).not.toHaveBeenCalled()

    // Typing the identifier voids the approval (it covers the exact payload),
    // so the checkbox is re-checked after the edit.
    fireEvent.change(screen.getByLabelText(/subject identifier/i), { target: { value: 'my-nvr-1.0' } })
    expect(screen.getByRole('button', { name: /push to greenwave/i })).toHaveProperty('disabled', true)
    approve()
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
    readyToPush()
    expect(screen.getByRole('button', { name: /push to greenwave/i })).toHaveProperty('disabled', false)
    fireEvent.change(screen.getByLabelText(/waiver comment/i), { target: { value: 'edited' } })
    expect(screen.getByRole('button', { name: /push to greenwave/i })).toHaveProperty('disabled', true)
  })

  it('voids the approval when the subject identifier is edited', () => {
    renderForm()
    readyToPush()
    expect(screen.getByRole('button', { name: /push to greenwave/i })).toHaveProperty('disabled', false)
    fireEvent.change(screen.getByLabelText(/subject identifier/i), { target: { value: 'other-nvr-2.0' } })
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
    expect(screen.getByText('ResultsDB: outcome per failure (PASSED / FAILED / INFO / NEEDS_INSPECTION)')).toBeDefined()
    expect(screen.getByText('WaiverDB: waiver for waivable failures')).toBeDefined()
  })

  it('shows waivers off and no job-name fallback in results-only mode', () => {
    renderForm({ greenwaveWaivers: false })
    readyToPush()
    expect(screen.getByText('Waivers: off')).toBeDefined()
    expect(screen.getByText('ResultsDB: outcome per failure (PASSED / FAILED / INFO / NEEDS_INSPECTION)')).toBeDefined()
    expect(screen.queryByText(/WaiverDB: waiver for waivable failures/)).toBeNull()
    // The identifier is what gets pushed — never the job name.
    expect(screen.getByText('Subject identifier: my-nvr-1.0')).toBeDefined()
  })

  it('shows only the ResultsDB line in results-only mode (waivers disabled)', () => {
    renderForm()
    expect(screen.getByText('ResultsDB: outcome per failure (PASSED / FAILED / INFO / NEEDS_INSPECTION)')).toBeDefined()
    expect(screen.queryByText(/WaiverDB/)).toBeNull()
    expect(screen.getByText(/I approve the text above\./)).toBeDefined()
    expect(screen.queryByText(/submitted with the waiver/)).toBeNull()
  })

  it('shows only the ResultsDB line when waivers are enabled but no identifier is given', () => {
    renderForm({ greenwaveWaivers: true })
    expect(screen.getByText('ResultsDB: outcome per failure (PASSED / FAILED / INFO / NEEDS_INSPECTION)')).toBeDefined()
    expect(screen.queryByText(/WaiverDB/)).toBeNull()
  })

  it('shows the WaiverDB line and composed-comment note when waivers will be written', () => {
    renderForm({ greenwaveWaivers: true })
    fireEvent.change(screen.getByLabelText(/subject identifier/i), { target: { value: 'my-nvr-1.0' } })
    expect(screen.getByText('ResultsDB: outcome per failure (PASSED / FAILED / INFO / NEEDS_INSPECTION)')).toBeDefined()
    expect(screen.getByText('WaiverDB: waiver for waivable failures')).toBeDefined()
    expect(screen.getByText(/The text above is included in the WaiverDB comment/)).toBeDefined()
    expect(screen.getByText(/I approve the text above\. It will be submitted with the waiver\./)).toBeDefined()
  })

  it('surfaces the server 422 detail on failure', async () => {
    postSpy.mockRejectedValueOnce(
      new ApiError(422, 'Unprocessable Entity', { detail: 'subject_identifier is required for waivers' }),
    )
    renderForm()
    readyToPush()
    fireEvent.click(screen.getByRole('button', { name: /push to greenwave/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent('subject_identifier is required for waivers')
    expect(screen.getByText(/Failed to push to Greenwave/)).toBeDefined()
  })

  it('calls onPushed with the result after a successful roundtrip', async () => {
    const onPushed = vi.fn()
    renderForm({ onPushed })
    readyToPush()
    fireEvent.click(screen.getByRole('button', { name: /push to greenwave/i }))

    await waitFor(() => expect(onPushed).toHaveBeenCalledOnce())
    expect(onPushed).toHaveBeenCalledWith({ success: true, message: 'pushed', pushed: 3, waived: 2, skipped: 1 })
  })

  it('calls onPushed with null when the request fails', async () => {
    postSpy.mockRejectedValueOnce(new ApiError(502, 'Bad Gateway', { detail: 'upstream down' }))
    const onPushed = vi.fn()
    renderForm({ onPushed })
    readyToPush()
    fireEvent.click(screen.getByRole('button', { name: /push to greenwave/i }))

    await waitFor(() => expect(onPushed).toHaveBeenCalledOnce())
    expect(onPushed).toHaveBeenCalledWith(null)
  })
})
