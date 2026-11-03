// jsdom has no layout engine (getBoundingClientRect returns zeros), so "same rendered row"
// cannot be measured here. This test pins the structure that makes the regression impossible:
// the identity row (h1 + status) and the model/summary row are separate siblings, and the model
// chip, token badge and actions are direct children of the summary row — so flex-wrap on a long
// job name can never pull them up into the badge row. Pixel verification lives in the approved
// mockup, not here.
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ReportPage } from '@/pages/ReportPage'
import { api } from '@/lib/api'
import type { ResultResponse, TokenUsageSummary } from '@/types'

vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
vi.mock('@/lib/useSSE', () => ({ useSSE: vi.fn() }))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ role: 'user', isOperator: false }) }))

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

const longJobName = `job-${'a'.repeat(236)}`

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

async function headerParts(jobName: string) {
  showReport(jobName)
  const heading = await screen.findByRole('heading', { level: 1 })
  const identityRow = heading.parentElement as HTMLElement
  const modelChip = screen.getByText('gemini / pro')
  const tokenBadge = screen.getByText(/100 in \/ 50 out/)
  const summaryRow = tokenBadge.parentElement as HTMLElement
  return { heading, identityRow, modelChip, tokenBadge, summaryRow }
}

describe('report header layout', () => {
  it.each(['example', longJobName])(
    'renders the model chip, token badge and actions as direct children of the summary row, below the identity row (%s)',
    async (jobName) => {
      const { heading, identityRow, modelChip, tokenBadge, summaryRow } = await headerParts(jobName)

      expect(heading).toHaveTextContent(jobName)
      expect(identityRow).toContainElement(heading)

      // summary row is a sibling of the identity row inside the same header container
      expect(summaryRow).not.toBe(identityRow)
      expect(summaryRow.parentElement).toBe(identityRow.parentElement)

      // direct children of the summary row, model before tokens
      const summaryChildren = Array.from(summaryRow.children)
      expect(summaryChildren).toContain(modelChip)
      expect(summaryChildren).toContain(tokenBadge)
      expect(summaryChildren.indexOf(modelChip)).toBeLessThan(summaryChildren.indexOf(tokenBadge))

      // never descendants of the identity row
      expect(identityRow).not.toContainElement(modelChip)
      expect(identityRow).not.toContainElement(tokenBadge)

      // action group belongs to the summary row, not the identity row
      const actionGroup = screen.getByRole('link', { name: /Chat/ }).parentElement as HTMLElement
      expect(summaryChildren).toContain(actionGroup)
      expect(identityRow).not.toContainElement(actionGroup)
    },
  )

  it('truncates the heading and keeps it in the identity row for a 240-char job name', async () => {
    expect(longJobName).toHaveLength(240)
    const { heading, identityRow } = await headerParts(longJobName)

    expect(heading.className).toContain('truncate')
    expect(heading).toHaveTextContent(longJobName)
    expect(heading.parentElement).toBe(identityRow)
  })

  it('renders the model label exactly once in the report header', async () => {
    showReport('example')
    await screen.findByRole('heading', { level: 1 })
    expect(screen.getAllByText('gemini / pro')).toHaveLength(1)
  })
})
