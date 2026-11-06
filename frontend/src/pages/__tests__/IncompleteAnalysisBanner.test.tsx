import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ReportPage } from '@/pages/ReportPage'
import { api } from '@/lib/api'
import { PROVIDER_FAILURE_DETAILS } from '@/lib/failureKeys'
import type { ResultResponse } from '@/types'

vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
vi.mock('@/lib/useSSE', () => ({ useSSE: vi.fn() }))

const auth = vi.hoisted(() => ({ isOperator: true }))
vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ role: auth.isOperator ? 'operator' : 'viewer', isOperator: auth.isOperator }),
}))

function showReport(
  incompleteCount: number,
  analysisState?: 'submitted' | 'analyzed',
  details: string | string[] = '',
  overrides: Array<{ test_name: string; classification: string }> = [],
  analysisError = 'AI analysis incomplete: response has no usable classification',
) {
  const response: ResultResponse = {
    job_id: 'job-1', jenkins_url: null, status: 'completed', created_at: '', base_url: null, result_url: null,
    result: {
      job_id: 'job-1', job_name: 'example', build_number: 1, jenkins_url: null,
      status: 'completed', summary: 'done', ai_provider: '', ai_model: '',
      analysis_state: analysisState,
      // The banner reads the failures, not `incomplete_count`: a reviewer
      // override classifies a failure without refetching the result.
      failures: Array.from({ length: incompleteCount }, (_, i) => ({
        id: `id-${i}`, test_name: `test-${i}`, error: 'boom',
        analysis: {
          classification: '', pattern: '', affected_tests: [],
          details: Array.isArray(details) ? (details[i] ?? '') : details,
          artifacts_evidence: '',
        },
        analysis_error: analysisError,
        error_signature: `sig-${i}`,
      })),
      child_job_analyses: [], request_params: { ai_provider: '', ai_model: '' },
      incomplete_count: incompleteCount,
    },
  }
  vi.mocked(api.get).mockImplementation(async path => {
    if (path === '/results/job-1') return response
    if (path === '/results/job-1/comments') return { comments: [], reviews: {} }
    if (path === '/api/ai-models') return { providers: {} }
    if (path.startsWith('/history/classifications')) {
      return {
        classifications: overrides.map(o => ({
          test_name: o.test_name, classification: o.classification, job_name: '',
          parent_job_name: '', reason: '', references_info: '', created_by: '',
          job_id: 'job-1', child_build_number: 0, created_at: '',
        })),
      }
    }
    throw new Error(`Unexpected request: ${path}`)
  })
  render(<MemoryRouter initialEntries={['/results/job-1']}><Routes>
    <Route path="/results/:jobId" element={<ReportPage />} />
    <Route path="/status/:jobId" element={<div>Status page</div>} />
  </Routes></MemoryRouter>)
}

describe('incomplete analysis banner', () => {
  it('shows the count and the Re-Analyze action for an operator', async () => {
    showReport(2)
    const banner = await screen.findByTestId('incomplete-analysis-banner')
    expect(banner).toHaveTextContent('2 failure(s) have no usable analysis')
    expect(within(banner).getByRole('button', { name: /Re-Analyze/ })).toBeInTheDocument()
  })

  it('hides the banner when nothing is incomplete', async () => {
    showReport(0)
    expect(await screen.findByText('example')).toBeInTheDocument()
    expect(screen.queryByTestId('incomplete-analysis-banner')).not.toBeInTheDocument()
  })

  it('hides the banner for a submitted job, whose analysis has not run', async () => {
    showReport(2, 'submitted')
    expect(await screen.findByText('Awaiting AI analysis')).toBeInTheDocument()
    expect(screen.queryByTestId('incomplete-analysis-banner')).not.toBeInTheDocument()
  })

  it('blames the provider when the AI call failed', async () => {
    // A provider failure stores the placeholder with no reason on the row.
    showReport(2, 'analyzed', 'Analysis failed; check server logs for details', [], '')
    const banner = await screen.findByTestId('incomplete-analysis-banner')
    expect(banner).toHaveTextContent('the AI provider call failed')
    expect(banner).not.toHaveTextContent('the model returned nothing')
  })

  it('does not blame the provider when the model returned nothing classifiable', async () => {
    // Same placeholder, but the row carries the reason the analysis is missing:
    // the answer arrived and carried no classification (#397).
    showReport(2, 'analyzed', PROVIDER_FAILURE_DETAILS)
    const banner = await screen.findByTestId('incomplete-analysis-banner')
    expect(banner).toHaveTextContent('the model returned nothing that could be classified')
    expect(banner).not.toHaveTextContent('the AI provider call failed')
    expect(banner).not.toHaveTextContent('had a failed AI provider call')
  })

  it('shows the banner for the same failures once an analysis produced nothing', async () => {
    showReport(2, 'analyzed')
    expect(await screen.findByTestId('incomplete-analysis-banner')).toHaveTextContent(
      '2 failure(s) have no usable analysis',
    )
  })

  it('does not blame the provider for failures a reviewer already classified', async () => {
    // A classified provider placeholder is no longer incomplete, so it must
    // not count towards the outage cause either.
    showReport(2, 'analyzed', [PROVIDER_FAILURE_DETAILS, ''], [
      { test_name: 'test-0', classification: 'INFRASTRUCTURE' },
    ])
    const banner = await screen.findByTestId('incomplete-analysis-banner')
    expect(banner).toHaveTextContent('1 failure(s) have no usable analysis')
    expect(banner).toHaveTextContent('the model returned nothing that could be classified')
    expect(banner).not.toHaveTextContent('the AI provider call failed')
    expect(banner).not.toHaveTextContent('had a failed AI provider call')
  })

  it('hides the Re-Analyze action from a viewer', async () => {
    auth.isOperator = false
    try {
      showReport(1)
      const banner = await screen.findByTestId('incomplete-analysis-banner')
      expect(banner).toHaveTextContent('1 failure(s) have no usable analysis')
      expect(within(banner).queryByRole('button', { name: /Re-Analyze/ })).not.toBeInTheDocument()
    } finally {
      auth.isOperator = true
    }
  })
})
