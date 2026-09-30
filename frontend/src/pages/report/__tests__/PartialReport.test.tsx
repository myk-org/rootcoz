import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ReportPage } from '@/pages/ReportPage'
import { api } from '@/lib/api'
import type { ResultResponse } from '@/types'

vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
vi.mock('@/lib/SSEProvider', () => ({ useSSE: vi.fn() }))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ role: 'viewer', isOperator: false }) }))

function showReport(childOnly = false) {
  const response: ResultResponse = {
    job_id: 'job-1', jenkins_url: null, status: 'completed', created_at: '', base_url: null, result_url: null,
    result: {
      job_id: 'job-1', job_name: 'example', build_number: 1, jenkins_url: null,
      status: 'completed', summary: '1 analyzed successfully. 1 group(s) failed; check server logs.',
      failed_analysis_groups: 1, passed_count: 3, ai_provider: '', ai_model: '',
      failures: [], child_job_analyses: childOnly ? [{
        id: 'leaf-2', job_name: 'leaf', build_number: 2, jenkins_url: null, summary: null,
        failures: [], failed_children: [], all_groups_failed: true,
        note: 'Child console analysis failed',
      }] : [],
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

describe('partial analysis report', () => {
  it('shows the persisted warning even when only a child failed', async () => {
    showReport(true)
    expect(await screen.findByRole('alert')).toHaveTextContent('1 group(s) failed')
    expect(screen.queryByText('All Tests Passed')).not.toBeInTheDocument()
  })

  it('opens the report and visibly warns of failed groups alongside successful tests', async () => {
    showReport()
    expect(await screen.findByText('example')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('1 group(s) failed')
    expect(screen.getByRole('button', { name: /Passed Tests.*3/ })).toBeInTheDocument()
    expect(screen.queryByText('All Tests Passed')).not.toBeInTheDocument()
    expect(screen.queryByText('Status page')).not.toBeInTheDocument()
  })
})
