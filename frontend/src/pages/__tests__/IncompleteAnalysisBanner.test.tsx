import { describe, expect, it, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ReportPage } from '@/pages/ReportPage'
import { api } from '@/lib/api'
import type { ResultResponse } from '@/types'

vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
vi.mock('@/lib/useSSE', () => ({ useSSE: vi.fn() }))

const auth = vi.hoisted(() => ({ isOperator: true }))
vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ role: auth.isOperator ? 'operator' : 'viewer', isOperator: auth.isOperator }),
}))

function showReport(incompleteCount: number | undefined) {
  const response: ResultResponse = {
    job_id: 'job-1', jenkins_url: null, status: 'completed', created_at: '', base_url: null, result_url: null,
    result: {
      job_id: 'job-1', job_name: 'example', build_number: 1, jenkins_url: null,
      status: 'completed', summary: 'done', ai_provider: '', ai_model: '',
      failures: [], child_job_analyses: [], request_params: { ai_provider: '', ai_model: '' },
      incomplete_count: incompleteCount,
    },
  }
  vi.mocked(api.get).mockImplementation(async path => {
    if (path === '/results/job-1') return response
    if (path === '/results/job-1/comments') return { comments: [], reviews: {} }
    if (path === '/api/ai-models') return { providers: {} }
    if (path.startsWith('/history/classifications')) return { classifications: [] }
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
