import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ReportPage } from '@/pages/ReportPage'
import { api } from '@/lib/api'
import type { ResultResponse } from '@/types'

vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
vi.mock('@/lib/useSSE', () => ({ useSSE: vi.fn() }))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ role: 'viewer', isOperator: false, isViewer: true }) }))

const resultResponse: ResultResponse = {
  job_id: 'job-1',
  jenkins_url: null,
  status: 'completed',
  created_at: '2026-11-20 14:00:00',
  base_url: null,
  result_url: null,
  result: {
    job_id: 'job-1',
    job_name: 'sort-job',
    build_number: 1,
    jenkins_url: null,
    status: 'completed',
    summary: '',
    ai_provider: '',
    ai_model: '',
    failures: [
      {
        id: 'f-dated',
        test_name: 'test-dated-vs-null',
        error: 'error-dated',
        analysis: { classification: 'CODE ISSUE', pattern: '', affected_tests: ['test-dated-vs-null'], details: '', artifacts_evidence: '' },
        error_signature: 'sig-dated',
      },
      {
        id: 'f-null',
        test_name: 'test-both-null',
        error: 'error-null',
        analysis: { classification: 'CODE ISSUE', pattern: '', affected_tests: ['test-both-null'], details: '', artifacts_evidence: '' },
        error_signature: 'sig-null',
      },
    ],
    child_job_analyses: [],
  },
}

describe('ReportPage classification sorting', () => {
  it('prefers newest dated classification over null timestamps and preserves API order for ties', async () => {
    vi.mocked(api.get).mockImplementation(async (path) => {
      if (path === '/results/job-1') return resultResponse
      if (path === '/results/job-1/comments') return { comments: [], reviews: {} }
      if (path === '/api/ai-models') return { providers: {} }
      if (path === '/results/job-1/enrich-comments') return { enrichments: {} }
      if (path.startsWith('/history/classifications')) {
        return {
          classifications: [
            { test_name: 'test-dated-vs-null', classification: 'INFRASTRUCTURE', job_name: '', parent_job_name: '', reason: '', references_info: '', created_by: 'alice', job_id: 'job-1', child_build_number: 0, created_at: null },
            { test_name: 'test-dated-vs-null', classification: 'CODE ISSUE', job_name: '', parent_job_name: '', reason: '', references_info: '', created_by: 'alice', job_id: 'job-1', child_build_number: 0, created_at: '2026-11-20 12:00:00' },
            { test_name: 'test-dated-vs-null', classification: 'PRODUCT BUG', job_name: '', parent_job_name: '', reason: '', references_info: '', created_by: 'alice', job_id: 'job-1', child_build_number: 0, created_at: '2026-11-20 14:27:56' },
            { test_name: 'test-both-null', classification: 'INFRASTRUCTURE', job_name: '', parent_job_name: '', reason: '', references_info: '', created_by: 'alice', job_id: 'job-1', child_build_number: 0, created_at: null },
            { test_name: 'test-both-null', classification: 'PRODUCT BUG', job_name: '', parent_job_name: '', reason: '', references_info: '', created_by: 'alice', job_id: 'job-1', child_build_number: 0, created_at: null },
          ],
        }
      }
      throw new Error(`Unexpected request: ${path}`)
    })

    render(
      <MemoryRouter initialEntries={['/report/job-1']}>
        <Routes>
          <Route path="/report/:jobId" element={<ReportPage />} />
        </Routes>
      </MemoryRouter>,
    )

    expect(await screen.findByText('test-dated-vs-null')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    const datedCard = screen.getByText('test-dated-vs-null').closest('[id]') as HTMLElement
    const nullCard = screen.getByText('test-both-null').closest('[id]') as HTMLElement

    await waitFor(() => {
      expect(within(datedCard).getByText('PRODUCT BUG')).toBeInTheDocument()
      expect(within(nullCard).getByText('INFRASTRUCTURE')).toBeInTheDocument()
    })
    expect(within(datedCard).queryByText('INFRASTRUCTURE')).not.toBeInTheDocument()
    expect(within(nullCard).queryByText('PRODUCT BUG')).not.toBeInTheDocument()
  })
})
