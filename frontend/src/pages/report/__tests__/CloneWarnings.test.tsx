import type { ReactNode } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor, cleanup } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import App from '@/App'
import { ReportPage } from '@/pages/ReportPage'
import { api } from '@/lib/api'
import type { ResultResponse } from '@/types'

Element.prototype.scrollIntoView = vi.fn()

const { onRefresh } = vi.hoisted(() => ({ onRefresh: { current: undefined as (() => Promise<void> | void) | undefined } }))
vi.mock('@/lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: { get: vi.fn(), post: vi.fn() },
}))
vi.mock('@/lib/useSSE', () => ({
  useSSE: (topic: string | null, handlers: Record<string, () => Promise<void> | void>) => {
    if (topic?.startsWith('results:')) onRefresh.current = handlers['status-changed']
  },
}))
vi.mock('@/lib/auth', () => ({
  useAuth: () => ({
    role: 'viewer',
    isOperator: false,
    authenticated: true,
    loading: false,
    isAdmin: false,
    canViewReports: false,
    username: 'viewer',
  }),
}))
vi.mock('@/lib/AuthProvider', () => ({ AuthProvider: ({ children }: { children: ReactNode }) => children }))
vi.mock('@/lib/SSEProvider', () => ({ SSEProvider: ({ children }: { children: ReactNode }) => children }))
vi.mock('@/components/layout/Layout', async () => {
  const { Outlet } = await import('react-router-dom')
  return { Layout: () => <Outlet /> }
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  onRefresh.current = undefined
  window.history.pushState({}, '', '/')
})

const base: ResultResponse = {
  job_id: 'job-1', jenkins_url: null, status: 'completed', created_at: '', base_url: null, result_url: null,
  result: { job_id: 'job-1', job_name: 'example', build_number: 1, jenkins_url: null,
    status: 'completed', summary: 'Analysis succeeded', ai_provider: '', ai_model: '', failures: [], child_job_analyses: [],
  },
}

type Log = NonNullable<NonNullable<ResultResponse['result']>['progress_log']>
function mockJobRoutes(getResponse: () => ResultResponse) {
  vi.mocked(api.get).mockImplementation(async (path) => {
    if (path === '/results/job-1') return getResponse()
    if (path === '/results/job-1/comments') return { comments: [], reviews: {} }
    if (path === '/api/ai-models') return { providers: {} }
    if (path.startsWith('/history/classifications')) return { classifications: [] }
    if (path === '/results/job-1/enrich-comments') return { enrichments: {} }
    throw new Error(`Unexpected request: ${path}`)
  })
}

function showReport(log?: Log, warnings?: string[]) {
  const response = { ...base, result: { ...base.result!, progress_log: log, source_warnings: warnings } }
  mockJobRoutes(() => response)
  render(<MemoryRouter initialEntries={['/results/job-1']}><Routes><Route path="/results/:jobId" element={<ReportPage />} /></Routes></MemoryRouter>)
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

it('shows the stored reason as the cause of a failed clone', async () => {
  showReport([
    { phase: 'cloning', repo: 'extra', state: 'failed', timestamp: 1, reason: 'Commit or ref not found' },
  ])
  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent(
    'Repository clone failed: extra — Commit or ref not found. Analysis results remain available below.',
  )
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

it('routes clone-failure reason from /status/:jobId to /results/:jobId in App and leaves /report/:jobId unmatched', async () => {
  const log: Log = [{ phase: 'cloning', repo: 'extra', state: 'failed', timestamp: 1, reason: 'Commit or ref not found' }]
  let current: ResultResponse = {
    ...base,
    status: 'running',
    result: { ...base.result!, status: 'running', progress_log: log },
  }
  mockJobRoutes(() => current)

  window.history.pushState({}, '', '/status/job-1')
  render(<App />)
  const repoLine = (await screen.findByText('extra')).closest('li')
  expect(repoLine).toHaveClass('text-signal-red')
  expect(repoLine).toHaveTextContent('Commit or ref not found')

  current = {
    ...base,
    status: 'completed',
    result: { ...base.result!, status: 'completed', progress_log: log },
  }
  await act(async () => { await onRefresh.current?.() })
  expect(window.location.pathname).toBe('/results/job-1')
  const alert = await screen.findByRole('alert')
  expect(alert).toHaveTextContent(
    'Repository clone failed: extra — Commit or ref not found. Analysis results remain available below.',
  )

  cleanup()
  window.history.pushState({}, '', '/report/job-1')
  const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
  try {
    await act(async () => { render(<App />) })
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText('Analysis succeeded')).toBeNull()
    expect(warnSpy.mock.calls.flat().join(' ')).toMatch(/No routes matched location "\/report\/job-1"/)
  } finally {
    warnSpy.mockRestore()
  }
})
