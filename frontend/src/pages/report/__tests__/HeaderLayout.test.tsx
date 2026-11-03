import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
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

function showReport(jobName: string) {
  const response: ResultResponse = {
    job_id: 'job-1', jenkins_url: null, status: 'completed', created_at: '', base_url: null, result_url: null,
    result: {
      job_id: 'job-1', job_name: jobName, build_number: 1, jenkins_url: null,
      status: 'completed', summary: '', ai_provider: 'gemini', ai_model: 'pro',
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
  render(<MemoryRouter initialEntries={['/report/job-1']}><Routes><Route path="/report/:jobId" element={<ReportPage />} /></Routes></MemoryRouter>)
}

describe('report header layout', () => {
  it.each(['example', 'a-very-long-job-name-that-wraps-the-first-row-on-narrow-viewports'])(
    'keeps the model chip and the token summary in the same row, below the identity row (%s)',
    async (jobName) => {
      showReport(jobName)
      const heading = await screen.findByRole('heading', { level: 1 })
      const identityRow = heading.parentElement as HTMLElement
      expect(identityRow).toContainElement(screen.getByText(jobName))

      const modelChip = screen.getByText('gemini / pro')
      const tokenBadge = screen.getByText(/100 in \/ 50 out/)

      expect(identityRow).not.toContainElement(modelChip)
      expect(identityRow).not.toContainElement(tokenBadge)

      const summaryRow = modelChip.parentElement as HTMLElement
      expect(summaryRow).not.toBe(identityRow)
      expect(summaryRow).toContainElement(tokenBadge)
    },
  )

  it('renders the model label exactly once in the report header', async () => {
    showReport('example')
    await screen.findByRole('heading', { level: 1 })
    expect(screen.getAllByText('gemini / pro')).toHaveLength(1)
  })
})
