import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { useRefreshEnrichments, useReportDispatch, useReportState } from '@/pages/report/reportState'
import { ReportProvider } from '../ReportContext'
import { reviewKey } from '@/lib/reviewKey'
import { api } from '@/lib/api'
import type { AnalysisResult, CommentEnrichment } from '@/types'

vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))

describe('reviewKey', () => {
  it('returns just testName when no child job', () => {
    expect(reviewKey('my.Test')).toBe('my.Test')
  })

  it('returns child format when child job provided', () => {
    expect(reviewKey('my.Test', 'child-job', 5)).toBe('child-job#5::my.Test')
  })

  it('returns just testName when childJobName is empty', () => {
    expect(reviewKey('my.Test', '', 0)).toBe('my.Test')
  })
})

/** Feeds SET_RESULT with the given forward links and shows the banner target. */
function ForwardLinkProbe({ reanalysisIds }: { reanalysisIds?: string[] }) {
  const state = useReportState()
  const dispatch = useReportDispatch()
  useEffect(() => {
    dispatch({
      type: 'SET_RESULT',
      payload: {
        result: {} as unknown as AnalysisResult,
        createdAt: '',
        completedAt: '',
        analysisStartedAt: '',
        reanalyzedToJobIds: reanalysisIds,
      },
    })
  }, [dispatch, reanalysisIds])
  return <span data-testid="forward">{state.reanalyzedToJobId}</span>
}

function renderForwardLink(reanalysisIds?: string[]) {
  return render(
    <ReportProvider>
      <ForwardLinkProbe reanalysisIds={reanalysisIds} />
    </ReportProvider>,
  )
}

describe('reanalyzedToJobId', () => {
  it('targets the latest re-analysis', () => {
    renderForwardLink(['re-1', 're-2'])
    expect(screen.getByTestId('forward')).toHaveTextContent('re-2')
  })

  it('is empty when every re-analysis was deleted', () => {
    // The backend unlinks deleted jobs, so an empty list means no banner.
    renderForwardLink([])
    expect(screen.getByTestId('forward')).toHaveTextContent('')
  })

  it('is empty when the job has no re-analyses', () => {
    renderForwardLink(undefined)
    expect(screen.getByTestId('forward')).toHaveTextContent('')
  })
})

/* ------------------------------------------------------------------ */
/*  Enrichments                                                        */
/* ------------------------------------------------------------------ */

const pr: CommentEnrichment = { type: 'github_pr', key: 'PR-1', status: 'open' }

/** Fetches for one or two targets and shows which targets landed in state. */
function EnrichmentProbe() {
  const state = useReportState()
  const refresh = useRefreshEnrichments()
  return (
    <>
      <button onClick={() => refresh('job-a')}>refresh a</button>
      <button onClick={() => { refresh('job-a'); refresh('job-b') }}>refresh twice</button>
      <span data-testid="enrichments">{Object.keys(state.enrichments).join(',')}</span>
    </>
  )
}

function renderEnrichments() {
  return render(
    <ReportProvider>
      <EnrichmentProbe />
    </ReportProvider>,
  )
}

describe('refreshEnrichments', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset()
    vi.mocked(api.post).mockReset()
  })

  it('reads enrichments over GET and dispatches them into state', async () => {
    vi.mocked(api.get).mockResolvedValue({ enrichments: { 'my.Test': [pr] } })
    renderEnrichments()

    fireEvent.click(screen.getByRole('button', { name: 'refresh a' }))

    await waitFor(() => expect(screen.getByTestId('enrichments')).toHaveTextContent('my.Test'))
    expect(api.get).toHaveBeenCalledWith('/results/job-a/enrich-comments')
    expect(api.post).not.toHaveBeenCalled()
  })

  it('drops a response that a newer request superseded', async () => {
    let resolveA!: (v: unknown) => void
    let resolveB!: (v: unknown) => void
    const pendingA = new Promise((res) => { resolveA = res })
    const pendingB = new Promise((res) => { resolveB = res })
    vi.mocked(api.get)
      .mockReturnValueOnce(pendingA as Promise<never>)
      .mockReturnValueOnce(pendingB as Promise<never>)
    renderEnrichments()

    // The second refresh queues behind the first one, invalidating its response.
    fireEvent.click(screen.getByRole('button', { name: 'refresh twice' }))
    expect(api.get).toHaveBeenCalledTimes(1)
    expect(api.get).toHaveBeenCalledWith('/results/job-a/enrich-comments')

    await act(async () => { resolveA({ enrichments: { 'job-a.Test': [pr] } }) })
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/results/job-b/enrich-comments'))
    expect(screen.getByTestId('enrichments')).toHaveTextContent('')

    await act(async () => { resolveB({ enrichments: { 'job-b.Test': [pr] } }) })
    await waitFor(() => expect(screen.getByTestId('enrichments')).toHaveTextContent('job-b.Test'))
    expect(screen.getByTestId('enrichments')).not.toHaveTextContent('job-a.Test')
  })
})
