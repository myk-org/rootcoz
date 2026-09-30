import { afterEach, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { ReportPage } from '@/pages/ReportPage'
import { api } from '@/lib/api'
import type { ResultResponse } from '@/types'

const { onRefresh } = vi.hoisted(() => ({ onRefresh: { current: undefined as (() => Promise<void>) | undefined } }))
vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), post: vi.fn() } }))
vi.mock('@/lib/SSEProvider', () => ({ useSSE: (topic: string, handlers: Record<string, () => Promise<void>>) => {
  if (topic.startsWith('results:')) onRefresh.current = handlers['status-changed']
} }))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ role: 'viewer', isOperator: false }) }))

afterEach(() => { cleanup(); vi.clearAllMocks(); onRefresh.current = undefined })

const base: ResultResponse = {
  job_id: 'job-1', jenkins_url: null, status: 'completed', created_at: '', base_url: null, result_url: null,
  result: { job_id: 'job-1', job_name: 'example', build_number: 1, jenkins_url: null,
    status: 'completed', summary: 'Analysis succeeded', ai_provider: '', ai_model: '', failures: [], child_job_analyses: [],
  },
}

type Log = NonNullable<NonNullable<ResultResponse['result']>['progress_log']>
function showReport(log?: Log, warnings?: string[]) {
  const response = { ...base, result: { ...base.result!, progress_log: log, source_warnings: warnings } }
  vi.mocked(api.get).mockImplementation(async (path) => {
    if (path === '/results/job-1') return response
    if (path === '/results/job-1/comments') return { comments: [], reviews: {} }
    if (path === '/api/ai-models') return { providers: {} }
    if (path.startsWith('/history/classifications')) return { classifications: [] }
    throw new Error(`Unexpected request: ${path}`)
  })
  vi.mocked(api.post).mockResolvedValue({ enrichments: {} })
  render(<MemoryRouter initialEntries={['/report/job-1']}><Routes><Route path="/report/:jobId" element={<ReportPage />} /></Routes></MemoryRouter>)
  return response
}

it('warns for only final failed clones, without leaking URLs or hiding successful analysis', async () => {
  showReport([
    { phase: 'cloning', repo: 'tests', state: 'failed', timestamp: 1, url: 'https://user:secret@example.com/tests' }, // pragma: allowlist secret
    { phase: 'cloning', repo: 'tests', state: 'cloned', timestamp: 2 },
    { phase: 'cloning', repo: 'extra', state: 'failed', timestamp: 3, ref: 'secret-ref' },
    { phase: 'routing', timestamp: 4 },
    { phase: 'cloning', repo: 'extra', state: 'failed', timestamp: 5 },
    { phase: 'cloning', repo: 'https://user:secret@example.com/unsafe', state: 'failed', timestamp: 6 }, // pragma: allowlist secret
  ], ['Other source issue'])
  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent('Repository clone failed: extra')
  expect(alert).toHaveTextContent('Other source issue')
  expect(alert.textContent?.match(/Repository clone failed:/g)).toHaveLength(1)
  expect(alert).not.toHaveTextContent('tests')
  expect(document.body).not.toHaveTextContent('secret')
  expect(screen.getByText('Analysis succeeded')).toBeInTheDocument()
})

it('drops a warning after a successful retry on result refresh', async () => {
  const response = showReport([{ phase: 'cloning', repo: 'extra', state: 'failed', timestamp: 1 }])
  await screen.findByRole('alert')
  response.result.progress_log?.push({ phase: 'cloning', repo: 'extra', state: 'cloning', timestamp: 2 }, { phase: 'cloning', repo: 'extra', state: 'cloned', timestamp: 3 })
  await act(async () => { await onRefresh.current?.() })
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  expect(screen.getByText('Analysis succeeded')).toBeInTheDocument()
})

it('does not invent a clone warning without a failed final state', async () => {
  showReport(undefined)
  await screen.findByText('Analysis succeeded')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
