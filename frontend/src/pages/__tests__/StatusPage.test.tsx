import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { api } from '@/lib/api'
import { StatusPage } from '../StatusPage'
import type { ResultResponse, TokenUsageSummary } from '@/types'

Element.prototype.scrollIntoView = vi.fn()

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
    get.mockResolvedValueOnce({ ...result('running', { ...usage, calls: [{ provider: 'gemini', model: 'test', call_type: 'analysis', credential_source: 'user', input_tokens: 100, output_tokens: 20, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 120, cost_usd: 0.25, duration_ms: 100 }] }), graft_estimated_tokens_saved: 1234 })
      .mockResolvedValueOnce({ ...result('running', { ...usage, total_input_tokens: 200, total_cost_usd: 0.5, calls: [{ provider: 'gemini', model: 'test', call_type: 'analysis', credential_source: 'server', input_tokens: 200, output_tokens: 20, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: 220, cost_usd: 0.5, duration_ms: 100 }] }), graft_estimated_tokens_saved: 2345 })
    renderPage()
    const badge = await screen.findByText(/100 in \/ 20 out · \$0.25/)
    expect(screen.getAllByText(/100 in \/ 20 out · \$0.25/)).toHaveLength(1)
    fireEvent.focus(badge)
    expect(screen.getAllByText('Credential source: User')[0]).toBeInTheDocument()
    expect(screen.getAllByText(/Estimated Graft tokens saved: 1,234, estimate versus reading whole referenced files, not billed AI tokens/)[0]).toBeInTheDocument()
    await act(async () => { onStatusChanged.current?.() })
    await waitFor(() => expect(screen.getByText(/200 in \/ 20 out · \$0.50/)).toBeInTheDocument())
    fireEvent.focus(screen.getByText(/200 in \/ 20 out · \$0.50/))
    expect(screen.getAllByText('Credential source: Server')[0]).toBeInTheDocument()
    expect(screen.getAllByText(/Estimated Graft tokens saved: 2,345, estimate versus/)[0]).toBeInTheDocument()
    expect(get).toHaveBeenCalledTimes(2)
    expect(get).toHaveBeenCalledWith('/results/job-1')
  })

  it('shows one savings-only badge without AI usage', async () => {
    get.mockResolvedValue({ ...result('running'), graft_estimated_tokens_saved: 1234 })
    renderPage()
    const badge = await screen.findByText(/Graft saved ~1,234 tokens/)
    expect(screen.getAllByText(/Graft saved ~1,234 tokens/)).toHaveLength(1)
    expect(badge.closest('div')?.parentElement).toHaveTextContent('GRAFT SAVINGS')
    expect(screen.queryByText('USAGE / COST')).not.toBeInTheDocument()
    expect(screen.queryByText(/in \/ .* out/)).not.toBeInTheDocument()
    fireEvent.focus(badge)
    expect(screen.getAllByText(/Estimated Graft tokens saved: 1,234, estimate versus reading whole referenced files, not billed AI tokens/)[0]).toBeInTheDocument()
    expect(screen.queryByText(/Credential source:/)).not.toBeInTheDocument()
  })

  it.each([0, undefined])('hides usage row for %s savings without usage', async saved => {
    get.mockResolvedValue({ ...result('running'), graft_estimated_tokens_saved: saved })
    renderPage()
    await waitFor(() => expect(screen.getByText('example')).toBeInTheDocument())
    expect(screen.queryByText('USAGE / COST')).not.toBeInTheDocument()
    expect(screen.queryByText(/Graft saved/)).not.toBeInTheDocument()
  })

  it('shows the aggregate source for a running job with no detailed calls', async () => {
    get.mockResolvedValue(result('running', { ...usage, credential_source: 'user', calls: [] }))
    renderPage()
    const badge = await screen.findByText(/100 in \/ 20 out/)
    fireEvent.focus(badge)
    expect(screen.getAllByText('Credential source: User')[0]).toBeInTheDocument()
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
    const badge = await screen.findByText(/100 in \/ 20 out · Unavailable/)
    expect(screen.queryByText(/\$0.00/)).not.toBeInTheDocument()
    fireEvent.focus(badge)
    expect(screen.getAllByText('Credential source: Unknown')[0]).toBeInTheDocument()
    expect(screen.queryByText(/estimate versus reading whole/)).not.toBeInTheDocument()
  })

  it('groups clone transitions in one chronological stage and refreshes repo states via SSE', async () => {
    const base = result('running')
    const log = [
      { phase: 'fetching', timestamp: 1 },
      { phase: 'cloning', repo: 'tests', state: 'cloning' as const, timestamp: 2 },
      { phase: 'cloning', repo: 'extra', state: 'cloning' as const, timestamp: 3 },
      { phase: 'cloning', repo: 'tests', state: 'cloned' as const, timestamp: 4 },
      { phase: 'routing', timestamp: 5 },
    ]
    const params = { ai_provider: 'gemini', ai_model: 'test', tests_repo_url: 'https://secret:token@example.com/tests.git', tests_repo_ref: 'main', additional_repos: [{ name: 'extra', url: 'https://user:pass@example.com/extra.git', ref: 'dev' }] } // pragma: allowlist secret
    get.mockResolvedValueOnce({ ...base, result: { ...base.result!, request_params: params, progress_phase: 'routing', progress_log: log } })
      .mockResolvedValueOnce({ ...base, result: { ...base.result!, request_params: params, progress_phase: 'routing', progress_log: [...log, { phase: 'cloning', repo: 'extra', state: 'failed', timestamp: 6 }] } })
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Cloning')
    const timeline = screen.getByText('Progress').parentElement!.parentElement!
    expect(timeline.textContent).toMatch(/Fetching test results.*Cloning.*Routing failure groups/)
    expect(screen.getAllByText('Cloning')).toHaveLength(1)
    expect(timeline.textContent).toMatch(/tests · https:\/\/example.com\/tests.git · main\s*cloned/)
    expect(timeline.textContent).toMatch(/extra · https:\/\/example.com\/extra.git · dev\s*cloning/)
    const toggle = screen.getByRole('button', { name: 'Cloning' })
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('extra')).toBeVisible()
    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(timeline.querySelector('#clone-repos')).toHaveAttribute('hidden')
    expect(screen.getByText('extra')).not.toBeVisible()
    expect(timeline).toHaveTextContent('Routing failure groups')
    expect(timeline).not.toHaveTextContent('secret')
    expect(timeline).not.toHaveTextContent('token')
    expect(timeline).not.toHaveTextContent('user:pass')
    await act(async () => { onStatusChanged.current?.() })
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(timeline.querySelector('#clone-repos')).toHaveAttribute('hidden')
    expect(screen.getByText('extra')).not.toBeVisible()
    toggle.focus()
    await user.keyboard('{Enter}')
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(timeline.querySelector('#clone-repos')).not.toHaveAttribute('hidden')
    await waitFor(() => expect(timeline.textContent).toMatch(/extra · https:\/\/example.com\/extra.git · dev\s*failed/))
    expect(screen.getByText('extra')).toBeVisible()
    expect(screen.getAllByText('Cloning')).toHaveLength(1)
    expect(screen.getByText('extra').closest('li')).toHaveClass('text-signal-red')
    expect(screen.getByText('failed')).toHaveClass('text-signal-red')
    const cloning = toggle.closest('[role="listitem"]')!
    expect(cloning).toHaveClass('text-signal-red')
    expect(cloning.querySelector('.text-signal-green')).toBeNull()
    expect(cloning.querySelector('.text-signal-red')).toHaveTextContent('!')
    expect(screen.getByText('tests').closest('li')).not.toHaveClass('text-signal-red')
    expect(screen.getAllByText('Routing failure groups...').at(-1)!.closest('[role="listitem"]')).not.toHaveClass('text-signal-red')
  })

  it('restores failed clone styling after page refresh without marking cancelled repos failed', async () => {
    const running = result('running')
    get.mockResolvedValue({ ...running, result: { ...running.result!, progress_log: [
      { phase: 'cloning', repo: 'broken', state: 'failed', timestamp: 1 },
      { phase: 'cloning', repo: 'stopped', state: 'cancelled', timestamp: 2 },
      { phase: 'routing', timestamp: 3 },
    ] } })
    renderPage()
    const cloning = (await screen.findByText('Cloning')).closest('[role="listitem"]')!
    expect(cloning).toHaveClass('text-signal-red')
    expect(cloning.querySelector('.text-signal-green')).toBeNull()
    expect(screen.getByText('broken').closest('li')).toHaveClass('text-signal-red')
    expect(screen.getByText('stopped').closest('li')).not.toHaveClass('text-signal-red')
    expect(screen.getAllByText('Routing failure groups...').at(-1)!.closest('[role="listitem"]')).not.toHaveClass('text-signal-red')
  })

  it('keeps the Cloning stage failed after a later successful transition for the same repo', async () => {
    const running = result('running')
    get.mockResolvedValue({ ...running, result: { ...running.result!, progress_log: [
      { phase: 'cloning', repo: 'tests', state: 'failed', timestamp: 1 },
      { phase: 'cloning', repo: 'tests', state: 'cloned', timestamp: 2 },
      { phase: 'routing', timestamp: 3 },
    ] } })
    renderPage()
    const cloning = (await screen.findByText('Cloning')).closest('[role="listitem"]')!
    expect(cloning).toHaveClass('text-signal-red')
    expect(cloning.querySelector('.text-signal-green')).toBeNull()
    expect(screen.getByText('tests').closest('li')).not.toHaveClass('text-signal-red')
  })

  it('marks an earlier failed phase red while a later phase is still running', async () => {
    const running = result('running')
    get.mockResolvedValue({ ...running, result: { ...running.result!, progress_log: [
      { phase: 'fetching', state: 'failed', timestamp: 1 },
      { phase: 'routing', timestamp: 2 },
    ] } })
    renderPage()
    const failed = (await screen.findAllByText('Fetching test results...')).at(-1)!.closest('[role="listitem"]')!
    expect(failed).toHaveClass('text-signal-red')
    expect(failed.querySelector('.text-signal-red')).toHaveTextContent('!')
    expect(failed.querySelector('.text-signal-green')).toBeNull()
    expect(screen.getAllByText('Routing failure groups...').at(-1)!.closest('[role="listitem"]')).not.toHaveClass('text-signal-red')
  })

  it('displays git clone URLs without credentials or query data, never as links, including rows with missing event URLs', async () => {
    const running = result('running')
    const gitUrl = 'git://user:password@example.com/org/tests.git?token=secret#private' // pragma: allowlist secret
    get.mockResolvedValue({ ...running, result: { ...running.result!,
      request_params: { tests_repo_url: gitUrl, tests_repo_ref: 'main', additional_repos: [
        { name: 'extra', url: 'git://user:password@example.com/org/extra.git?token=secret#private', ref: 'dev' }, // pragma: allowlist secret
      ] },
      progress_log: [
        { phase: 'cloning', repo: 'tests', state: 'cloning', timestamp: 1 },
        { phase: 'cloning', repo: 'extra', state: 'cloning', url: 'git://other:credentials@example.com/org/extra.git?key=hidden#fragment', timestamp: 2 }, // pragma: allowlist secret
      ],
    } })
    renderPage()
    const list = await screen.findByRole('list', { name: 'Progress' })
    expect(screen.getByText('TEST REPO').parentElement).toHaveTextContent('git://example.com/org/tests.git:main')
    expect(screen.getByText('TEST REPO').parentElement?.querySelector('a')).toBeNull()
    expect(screen.getByText('tests').closest('li')).toHaveTextContent('git://example.com/org/tests.git · main')
    expect(screen.getByText('extra').closest('li')).toHaveTextContent('git://example.com/org/extra.git · dev')
    expect(list.querySelectorAll('a')).toHaveLength(0)
    expect(screen.getByTestId('status-card').textContent).not.toMatch(/password|secret|private|credentials|hidden|fragment|Invalid repository URL|URL unavailable/)
  })

  it('keeps invalid schemes hidden while preserving sanitized HTTP links', async () => {
    const running = result('running')
    get.mockResolvedValue({ ...running, result: { ...running.result!,
      request_params: { tests_repo_url: 'https://user:pass@example.com/tests.git?token=secret#private' }, // pragma: allowlist secret
      progress_log: [{ phase: 'cloning', repo: 'tests', state: 'cloning', timestamp: 1 },
        { phase: 'cloning', repo: 'bad', state: 'cloning', url: 'javascript:alert(1)', timestamp: 2 }],
    } })
    renderPage()
    await screen.findByText('Cloning')
    expect(screen.getAllByRole('link', { name: 'https://example.com/tests.git' })).toHaveLength(2)
    for (const link of screen.getAllByRole('link', { name: 'https://example.com/tests.git' })) {
      expect(link).toHaveAttribute('href', 'https://example.com/tests.git')
    }
    expect(screen.getByText('bad').closest('li')).toHaveTextContent('URL unavailable')
    expect(screen.getByTestId('status-card').textContent).not.toMatch(/password|token|private|javascript/)
  })

  it('groups legacy clone snapshots on page load', async () => {
    const base = result('running')
    get.mockResolvedValue({ ...base, result: { ...base.result!, progress_log: [
      { phase: 'cloning', repos: ['tests'], timestamp: 1 },
      { phase: 'cloning', repos: [], timestamp: 2 },
    ] } })
    renderPage()
    await screen.findByText('Cloning')
    expect(screen.getAllByText('Cloning')).toHaveLength(1)
    expect(screen.getByText('tests').parentElement).toHaveTextContent('URL unavailable · default')
    expect(screen.getByText('tests').parentElement).toHaveTextContent('cloned')
  })

  it('shows one row per concurrent clone with its URL, ref and latest state, preserving other stages', async () => {
    const running = result('running')
    get.mockResolvedValue({ ...running, result: { ...running.result!, progress_phase: 'cloning',
      request_params: { ai_provider: 'gemini', ai_model: 'test', additional_repos: [
        { name: 'alpha', url: 'https://secret:password@example.com/org/alpha.git', ref: 'release/very-long-branch-name' }, // pragma: allowlist secret
        { name: 'beta', url: 'https://example.com/org/beta.git', ref: 'main' },
      ] },
      progress_log: [
        { phase: 'fetching', timestamp: 1 },
        { phase: 'cloning', repo: 'alpha', state: 'cloning', timestamp: 2 },
        { phase: 'cloning', repo: 'beta', state: 'cloning', timestamp: 3 },
        { phase: 'cloning', repo: 'alpha', state: 'cloned', timestamp: 4 },
      ],
    } })
    renderPage()
    const list = await screen.findByRole('list', { name: 'Progress' })
    expect(list).toHaveAttribute('tabindex', '0')
    expect(list).toHaveClass('overflow-y-auto')
    expect(screen.getByText('Fetching test results...')).toBeInTheDocument()
    expect(screen.getAllByText('alpha')).toHaveLength(1)
    expect(screen.getAllByText('beta')).toHaveLength(1)
    expect(screen.getByText('cloned')).toBeInTheDocument()
    expect(screen.getByText('cloning')).toBeInTheDocument()
    expect(screen.getByText('alpha').closest('li')).toHaveTextContent('release/very-long-branch-name')
    expect(screen.getByRole('link', { name: 'https://example.com/org/alpha.git' })).toHaveAttribute('href', 'https://example.com/org/alpha.git')
    expect(screen.queryByText(/password/)).not.toBeInTheDocument()
    expect(list.closest('[data-testid="status-card"]')).toHaveClass('w-full', 'min-w-0')
  })

  it('uses event metadata over stale request params', async () => {
    const running = result('running')
    get.mockResolvedValue({ ...running, result: { ...running.result!,
      request_params: { ai_provider: 'gemini', ai_model: 'test', additional_repos: [{ name: 'alpha', url: 'https://old.example/alpha', ref: 'old' }] },
      progress_log: [{ phase: 'cloning', repo: 'alpha', state: 'cloning', url: 'https://new.example/alpha', ref: 'new', timestamp: 1 }],
    } })
    renderPage()
    const row = (await screen.findByText('alpha')).closest('li')!
    expect(row).toHaveTextContent('new')
    expect(row.querySelector('a')).toHaveAttribute('href', 'https://new.example/alpha')
    expect(row).not.toHaveTextContent('old')
  })

  it('prefers persisted event metadata without request params and sanitizes links on live updates', async () => {
    const running = result('running')
    const longRef = `release/${'long-'.repeat(30)}`
    const longPath = `org/${'nested/'.repeat(30)}repo.git`
    const log = [
      { phase: 'cloning', repo: 'alpha', state: 'cloning' as const, url: `https://user:password@example.com/${longPath}?token=secret#private`, ref: longRef, timestamp: 1 }, // pragma: allowlist secret
      { phase: 'cloning', repo: 'beta', state: 'cloning' as const, url: 'javascript:alert(1)', timestamp: 2 },
    ]
    get.mockResolvedValueOnce({ ...running, result: { ...running.result!, progress_log: log } })
      .mockResolvedValueOnce({ ...running, result: { ...running.result!, progress_log: [...log, { phase: 'cloning', repo: 'alpha', state: 'cloned', timestamp: 3 }, { phase: 'routing', timestamp: 4 }] } })
    renderPage()
    const list = await screen.findByRole('list', { name: 'Progress' })
    const alpha = screen.getByText('alpha').closest('li')!
    const beta = screen.getByText('beta').closest('li')!
    expect(alpha).toHaveTextContent(longRef)
    expect(alpha).toHaveClass('min-w-0', 'break-words')
    expect(alpha.querySelector('a')).toHaveClass('break-all')
    expect(alpha.querySelector('a')).toHaveAttribute('href', `https://example.com/${longPath}`)
    expect(beta).toHaveTextContent('URL unavailable · default')
    expect(beta.querySelector('a')).toBeNull()
    expect(list.closest('[data-testid="status-card"]')).toHaveClass('w-full', 'max-w-[80rem]')
    expect(list).toHaveClass('max-h-[min(60vh,36rem)]')
    expect(list.textContent).not.toMatch(/password|secret|private|javascript/)
    await act(async () => { onStatusChanged.current?.() })
    await waitFor(() => expect(alpha).toHaveTextContent('cloned'))
    expect(alpha).toHaveTextContent(longRef)
    expect(screen.getAllByText('Cloning')).toHaveLength(1)
    expect(list).toHaveTextContent('Routing failure groups')
  })

  it('updates clone rows on SSE refresh and handles legacy snapshots without generic cloning spam', async () => {
    const running = result('running')
    const base = { ...running.result!, request_params: { ai_provider: 'gemini', ai_model: 'test', additional_repos: [
      { name: 'alpha', url: 'javascript:alert(1)', ref: 'main' },
      { name: 'beta', url: 'https://example.com/beta', ref: 'dev' },
    ] } }
    get.mockResolvedValueOnce({ ...running, result: { ...base, progress_log: [
      { phase: 'cloning', timestamp: 1 },
      { phase: 'cloning', repos: ['alpha', 'beta'], timestamp: 2 },
    ] } }).mockResolvedValueOnce({ ...running, result: { ...base, progress_log: [
      { phase: 'cloning', timestamp: 1 },
      { phase: 'cloning', repos: ['alpha', 'beta'], timestamp: 2 },
      { phase: 'cloning', repo: 'alpha', state: 'failed', timestamp: 3 },
      { phase: 'cloning', repo: 'beta', state: 'cloned', timestamp: 4 },
    ] } })
    renderPage()
    await waitFor(() => expect(screen.getAllByText('alpha')).toHaveLength(1))
    expect(screen.getAllByText('beta')).toHaveLength(1)
    expect(screen.queryByRole('link', { name: /javascript/ })).not.toBeInTheDocument()
    expect(screen.queryAllByText('cloning')).toHaveLength(2)
    await act(async () => { onStatusChanged.current?.() })
    await waitFor(() => expect(screen.getByText('failed')).toBeInTheDocument())
    expect(screen.getByText('cloned')).toBeInTheDocument()
    expect(screen.getAllByText('alpha')).toHaveLength(1)
    expect(screen.queryAllByText('cloning')).toHaveLength(0)
  })

  it.each([
    ['fetching', 'Fetching test results...'],
    ['routing', 'Routing failure groups...'],
    ['agent_routing', 'Routing failure groups to agents...'],
    ['analyzing_failures (group 2/3)', 'Analyzing test failures — group 2/3...'],
    ['analyzing_failures (group 1/1)', 'Analyzing test failures — group 1/1...'],
    ['analyzing_failures', 'Analyzing test failures...'],
    ['cross_failure', 'Finding cross-failure patterns...'],
  ])('labels %s progress', async (phase, label) => {
    get.mockResolvedValue({ ...result('running'), result: { ...result('running').result!, progress_phase: phase, progress_log: [{ phase, timestamp: 1 }] } })
    renderPage()
    expect((await screen.findAllByText(label)).length).toBe(2)
    expect(screen.queryByText(phase)).not.toBeInTheDocument()
  })

  it('falls back to the raw phase in both current progress and log for unknown groups', async () => {
    const phase = 'analyzing_failures (group unknown)'
    get.mockResolvedValue({ ...result('running'), result: { ...result('running').result!, progress_phase: phase, progress_log: [{ phase, timestamp: 1 }] } })
    renderPage()
    expect((await screen.findAllByText(phase)).length).toBe(2)
  })

  it('does not show cost when there is no usage', async () => {
    get.mockResolvedValue(result('running'))
    renderPage()
    await waitFor(() => expect(screen.getByText('Analysis in progress')).toBeInTheDocument())
    expect(screen.queryByText('USAGE / COST')).not.toBeInTheDocument()
  })
})
