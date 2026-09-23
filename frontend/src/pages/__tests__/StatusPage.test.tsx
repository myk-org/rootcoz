import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { api } from '@/lib/api'
import { StatusPage } from '../StatusPage'
import type { ResultResponse, TokenUsageSummary } from '@/types'

const { onStatusChanged } = vi.hoisted(() => ({ onStatusChanged: { current: undefined as (() => void) | undefined } }))
vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() }, ApiError: class extends Error {} }))
vi.mock('@/lib/SSEProvider', () => ({ useSSE: (_topic: string, handlers: Record<string, () => void>) => { onStatusChanged.current = handlers['status-changed'] } }))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ isAdmin: false, isOperator: false, username: 'viewer' }) }))

const usage: TokenUsageSummary = {
  total_input_tokens: 100, total_output_tokens: 20, total_cache_read_tokens: 0,
  total_cache_write_tokens: 0, total_tokens: 120, total_cost_usd: 0.25,
  total_duration_ms: 100, total_calls: 1, calls: [],
}
const result = (status: ResultResponse['status'], token_usage?: TokenUsageSummary): ResultResponse => ({
  job_id: 'job-1', jenkins_url: null, status, created_at: '2026-01-01T00:00:00Z',
  base_url: null, result_url: null, result: {
    job_id: 'job-1', job_name: 'example', build_number: 1, jenkins_url: null,
    status, summary: '', ai_provider: 'gemini', ai_model: 'test', failures: [],
    child_job_analyses: [], token_usage,
  },
})
const get = vi.mocked(api.get)
function renderPage() {
  render(<MemoryRouter initialEntries={['/status/job-1']}><Routes><Route path="/status/:jobId" element={<StatusPage />} /></Routes></MemoryRouter>)
}

describe('StatusPage usage', () => {
  beforeEach(() => { vi.clearAllMocks(); onStatusChanged.current = undefined })

  it('shows live tokens and cost, refreshing on status SSE events', async () => {
    get.mockResolvedValueOnce(result('running', usage)).mockResolvedValueOnce(result('running', { ...usage, total_input_tokens: 200, total_cost_usd: 0.5 }))
    renderPage()
    await waitFor(() => expect(screen.getByText(/100 in \/ 20 out · \$0.25/)).toBeInTheDocument())
    await act(async () => { onStatusChanged.current?.() })
    await waitFor(() => expect(screen.getByText(/200 in \/ 20 out · \$0.50/)).toBeInTheDocument())
    expect(get).toHaveBeenCalledTimes(2)
    expect(get).toHaveBeenCalledWith('/results/job-1')
  })

  it.each(['failed', 'aborted'] as const)('retains usage for %s jobs', async status => {
    get.mockResolvedValue(result(status, usage))
    renderPage()
    await waitFor(() => expect(screen.getByText(/100 in \/ 20 out · \$0.25/)).toBeInTheDocument())
  })

  it('shows actual zero cost when known', async () => {
    get.mockResolvedValue(result('running', { ...usage, total_cost_usd: 0 }))
    renderPage()
    await waitFor(() => expect(screen.getByText(/100 in \/ 20 out · \$0.00/)).toBeInTheDocument())
  })

  it('shows Unavailable for unknown cost, not zero', async () => {
    get.mockResolvedValue(result('running', { ...usage, total_cost_usd: null }))
    renderPage()
    await waitFor(() => expect(screen.getByText(/100 in \/ 20 out · Unavailable/)).toBeInTheDocument())
    expect(screen.queryByText(/\$0.00/)).not.toBeInTheDocument()
  })

  it('does not show cost when there is no usage', async () => {
    get.mockResolvedValue(result('running'))
    renderPage()
    await waitFor(() => expect(screen.getByText('Analysis in progress')).toBeInTheDocument())
    expect(screen.queryByText('USAGE / COST')).not.toBeInTheDocument()
  })
})
