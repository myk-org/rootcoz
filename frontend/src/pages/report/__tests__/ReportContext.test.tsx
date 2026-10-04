import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { useEffect } from 'react'
import { useReportDispatch, useReportState } from '@/pages/report/reportState'
import { ReportProvider } from '../ReportContext'
import { reviewKey } from '@/lib/reviewKey'
import type { AnalysisResult } from '@/types'

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
