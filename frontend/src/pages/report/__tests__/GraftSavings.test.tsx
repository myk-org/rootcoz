import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ReportPage } from '@/pages/ReportPage'
import { api } from '@/lib/api'
import type { ResultResponse, TokenUsageSummary } from '@/types'

vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
vi.mock('@/lib/useSSE', () => ({ useSSE: vi.fn() }))
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

function showReport(graftEstimatedTokensSaved?: number, tokenUsage?: TokenUsageSummary) {
  const response: ResultResponse = {
    job_id: 'job-1', jenkins_url: null, status: 'completed', created_at: '',
    base_url: null, result_url: null,
    ...(graftEstimatedTokensSaved === undefined ? {} : { graft_estimated_tokens_saved: graftEstimatedTokensSaved }),
    result: {
      job_id: 'job-1', job_name: 'example', build_number: 1, jenkins_url: null,
      status: 'completed', summary: '', ai_provider: '', ai_model: '',
      failures: [], child_job_analyses: [], token_usage: tokenUsage,
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
  it('shows Graft only inside the total usage tooltip, without changing billed usage or cost', async () => {
    showReport(1234, usage)
    const badge = await screen.findByText(/100 in \/ 50 out/)
    expect(screen.getAllByText(/100 in \/ 50 out/)).toHaveLength(1)
    expect(badge).toHaveTextContent('$0.02')
    expect(screen.queryByText(/Estimated Graft tokens saved/)).not.toBeInTheDocument()
    fireEvent.focus(badge)
    expect(screen.getAllByText('Total tokens: 150')[0]).toBeInTheDocument()
    expect(screen.getAllByText(/Estimated Graft tokens saved: 1,234, estimate versus reading whole referenced files, not billed AI tokens/)[0]).toBeInTheDocument()
    expect(screen.getAllByText('Credential source: Unknown')[0]).toBeInTheDocument()
  })

  it.each([0, undefined])('does not show a savings badge for %s', async (saved) => {
    showReport(saved, usage)
    await waitFor(() => expect(screen.getByText('example')).toBeInTheDocument())
    expect(screen.queryByText(/Estimated Graft tokens saved/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/Graft saved/)).not.toBeInTheDocument()
    const badge = screen.getByText(/100 in \/ 50 out/)
    expect(badge).toHaveTextContent('$0.02')
    fireEvent.focus(badge)
    expect(screen.getAllByText('Credential source: Unknown')[0]).toBeInTheDocument()
    expect(screen.queryByText(/1,234, estimate versus/)).not.toBeInTheDocument()
  })

  it('shows one job-level savings-only badge without AI usage', async () => {
    showReport(1234)
    const badge = await screen.findByText(/Graft saved ~1,234 tokens/)
    expect(screen.getAllByText(/Graft saved ~1,234 tokens/)).toHaveLength(1)
    expect(screen.queryByText(/in \/ .* out/)).not.toBeInTheDocument()
    fireEvent.focus(badge)
    expect(screen.getAllByText(/Estimated Graft tokens saved: 1,234, estimate versus reading whole referenced files, not billed AI tokens/)[0]).toBeInTheDocument()
    expect(screen.queryByText(/Credential source:/)).not.toBeInTheDocument()
  })

  it.each([0, undefined])('hides savings-only badge for %s without usage', async saved => {
    showReport(saved)
    await waitFor(() => expect(screen.getByText('example')).toBeInTheDocument())
    expect(screen.queryByText(/Graft saved|Estimated Graft tokens saved/)).not.toBeInTheDocument()
  })

  it('prefers the SQL aggregate even when detailed calls are absent', async () => {
    showReport(undefined, { ...usage, credential_source: 'mixed', calls: [] })
    const badge = await screen.findByText(/100 in \/ 50 out/)
    fireEvent.focus(badge)
    expect(screen.getAllByText('Credential source: Mixed')[0]).toBeInTheDocument()
  })

  it.each([
    [['user'], 'User'],
    [['server'], 'Server'],
    [['user', 'server'], 'Mixed'],
    [['user', 'unknown'], 'Unknown'],
    [['server', undefined], 'Unknown'],
    [[], 'Unknown'],
  ] as const)('derives legacy source for %j as %s', async (sources, expected) => {
    showReport(undefined, { ...usage, calls: sources.map(source => ({
      provider: 'gemini', model: 'test', call_type: 'analysis', credential_source: source,
      input_tokens: 10, output_tokens: 5, cache_read_tokens: 0, cache_write_tokens: 0,
      total_tokens: 15, cost_usd: 0.01, duration_ms: 0,
    })) })
    const badge = await screen.findByText(/100 in \/ 50 out/)
    fireEvent.focus(badge)
    expect(screen.getAllByText(`Credential source: ${expected}`)[0]).toBeInTheDocument()
    if (sources.length) expect(screen.getAllByText(/gemini\/test ·/)).toHaveLength(sources.length * 2)
  })
})
