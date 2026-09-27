import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ReportPage } from '@/pages/ReportPage'
import { api } from '@/lib/api'
import type { ResultResponse, TokenUsageSummary } from '@/types'

vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
vi.mock('@/lib/SSEProvider', () => ({ useSSE: vi.fn() }))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ role: 'viewer', isOperator: false }) }))

const usage: TokenUsageSummary = {
  total_input_tokens: 100,
  total_output_tokens: 50,
  total_tokens: 150,
  total_cache_read_tokens: 0,
  total_cache_write_tokens: 0,
  total_calls: 1,
  total_duration_ms: 0,
  total_cost_usd: 0.02,
  calls: [],
}

function showReport(graftEstimatedTokensSaved?: number) {
  const response: ResultResponse = {
    job_id: 'job-1', jenkins_url: null, status: 'completed', created_at: '',
    base_url: null, result_url: null,
    ...(graftEstimatedTokensSaved === undefined ? {} : { graft_estimated_tokens_saved: graftEstimatedTokensSaved }),
    result: {
      job_id: 'job-1', job_name: 'example', build_number: 1, jenkins_url: null,
      status: 'completed', summary: '', ai_provider: '', ai_model: '',
      failures: [], child_job_analyses: [], token_usage: usage,
    },
  }
  vi.mocked(api.get).mockImplementation(async (path) => {
    if (path === '/results/job-1') return response
    if (path === '/results/job-1/comments') return { comments: [], reviews: {} }
    if (path === '/api/ai-models') return { providers: {} }
    if (path.startsWith('/history/classifications')) return { classifications: [] }
    throw new Error(`Unexpected request: ${path}`)
  })
  vi.mocked(api.post).mockResolvedValue({ enrichments: {} })
  return render(<MemoryRouter initialEntries={['/report/job-1']}><Routes><Route path="/report/:jobId" element={<ReportPage />} /></Routes></MemoryRouter>)
}

describe('report Graft estimate', () => {
  it('shows a separate estimated savings badge with an explanation, without changing billed usage or cost', async () => {
    showReport(1234)
    const badge = await screen.findByText(/Estimated Graft tokens saved: 1\.2k/i)
    expect(screen.getByText(/100 in \/ 50 out/)).toHaveTextContent('$0.02')
    fireEvent.focus(badge)
    expect((await screen.findAllByText(/estimate versus reading whole referenced files, not billed AI tokens/i)).length).toBeGreaterThan(0)
    fireEvent.blur(badge)
    fireEvent.focus(screen.getByText(/100 in \/ 50 out/))
    expect((await screen.findAllByText('Total tokens: 150')).length).toBeGreaterThan(0)
  })

  it.each([0, undefined])('does not show a savings badge for %s', async (saved) => {
    showReport(saved)
    await waitFor(() => expect(screen.getByText('example')).toBeInTheDocument())
    expect(screen.queryByText(/Estimated Graft tokens saved/i)).not.toBeInTheDocument()
    expect(screen.getByText(/100 in \/ 50 out/)).toHaveTextContent('$0.02')
  })
})
