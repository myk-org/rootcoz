import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { AuthProvider } from '@/lib/auth'
import { ApiError } from '@/lib/api'
import { NewAnalysisPage } from '@/pages/NewAnalysisPage'
import { ReAnalyzeDialog } from '@/pages/report/ReAnalyzeDialog'
import { resetProviderCatalogCache } from '@/lib/useProviderOptions'
import type { AnalysisResult } from '@/types'

const get = vi.fn()
const post = vi.fn()
vi.mock('@/lib/api', async (importActual) => ({
  ...await importActual<typeof import('@/lib/api')>(),
  api: { get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args) },
}))

let granted = false
const serverModel = { id: 'sonnet', name: 'Sonnet', provider: 'claude', credential_sources: ['server'] }
const userModel = { id: 'gpt', name: 'GPT', provider: 'openai', credential_sources: ['user'] }
const defaults = {
  ai_provider: 'claude', ai_model: 'sonnet', force_server_credentials: true,
  ai_call_timeout: 10, tests_repo_url: '', additional_repos: [], peer_ai_configs: [],
  peer_analysis_max_rounds: 3, jira_enabled: false, jira_url: '', jira_project_key: '',
  get_job_artifacts: false, jenkins_artifacts_max_size_mb: 50, wait_for_completion: false,
  poll_interval_minutes: 2, max_wait_minutes: 0,
}
function setup() {
  get.mockImplementation(async (path: string) => {
    if (path === '/api/auth/me') return { username: 'alice', role: 'reviewer', is_admin: false, can_view_reports: true, can_use_server_providers: granted }
    if (path === '/api/user/tokens') return { github_token: '', jira_email: '', jira_token: '' }
    if (path.startsWith('/api/ai-models')) return { providers: { claude: [serverModel], openai: [userModel] }, provider_status: {} }
    return defaults
  })
  post.mockResolvedValue({ job_id: 'next' })
}

function page(result: AnalysisResult, showDialog: boolean) {
  return <AuthProvider><MemoryRouter>
    <section aria-label="New analysis"><NewAnalysisPage /></section>
    {showDialog && <ReAnalyzeDialog open onOpenChange={() => {}} result={result} jobId="job" />}
  </MemoryRouter></AuthProvider>
}

// Radix requires pointer APIs absent from jsdom.
HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

afterEach(() => { act(() => resetProviderCatalogCache()); get.mockReset(); post.mockReset(); granted = false })

it('updates a mounted new-analysis picker and submit after grant and revoke without losing the server default', async () => {
  setup()
  const user = userEvent.setup()
  render(<AuthProvider><MemoryRouter><NewAnalysisPage /></MemoryRouter></AuthProvider>)
  await user.click(await screen.findByRole('button', { name: 'AI Configuration' }))
  await user.click(screen.getByRole('button', { name: 'Paste XML' }))
  await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
  expect(screen.getByRole('button', { name: 'Submit Analysis' })).toBeDisabled()
  expect(screen.getByRole('switch', { name: 'Use server credentials' })).toBeDisabled()
  granted = true
  act(() => window.dispatchEvent(new Event('focus')))
  await waitFor(() => expect(screen.getByRole('switch', { name: 'Use server credentials' })).toHaveAttribute('aria-checked', 'true'))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Submit Analysis' })).toBeEnabled())
  expect(get).toHaveBeenCalledWith('/api/ai-models?force_server_credentials=true')
  granted = false
  act(() => window.dispatchEvent(new Event('focus')))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Submit Analysis' })).toBeDisabled())
  expect(screen.getByRole('switch', { name: 'Use server credentials' })).toBeDisabled()
  await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
  expect(screen.getByRole('option', { name: /Claude · Server$/ })).toHaveAttribute('data-disabled')
  expect(post).not.toHaveBeenCalled()
})

it('ignores an older granted /me after a newer revocation on mounted analysis forms', async () => {
  setup()
  granted = true
  const result = { request_params: { ai_provider: 'claude', ai_model: 'sonnet', force_server_credentials: true } } as unknown as AnalysisResult
  const { container, rerender } = render(page(result, false))
  const newForm = container.querySelector('section')!
  const user = userEvent.setup()
  await user.click(await within(newForm).findByRole('button', { name: 'AI Configuration' }))
  await user.click(within(newForm).getByRole('button', { name: 'Paste XML' }))
  await user.type(within(newForm).getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
  await waitFor(() => expect(within(newForm).getByRole('button', { name: 'Submit Analysis' })).toBeEnabled())
  rerender(page(result, true))
  const reForm = await screen.findByRole('dialog')
  await waitFor(() => expect(within(reForm).getByRole('button', { name: 'Re-Analyze' })).toBeEnabled())

  let resolveOlder!: (value: unknown) => void
  let resolveNewer!: (value: unknown) => void
  const older = new Promise((resolve) => { resolveOlder = resolve })
  const newer = new Promise((resolve) => { resolveNewer = resolve })
  let meCalls = 0
  get.mockImplementation((path: string) => {
    if (path === '/api/auth/me') return ++meCalls === 1 ? older : newer
    if (path === '/api/user/tokens') return Promise.resolve({ github_token: '', jira_email: '', jira_token: '' })
    if (path.startsWith('/api/ai-models')) return Promise.resolve({ providers: { claude: [serverModel], openai: [userModel] }, provider_status: {} })
    return Promise.resolve(defaults)
  })
  act(() => window.dispatchEvent(new Event('focus')))
  act(() => window.dispatchEvent(new Event('focus')))
  await act(async () => { resolveNewer({ username: 'alice', role: 'reviewer', is_admin: false, can_view_reports: true, can_use_server_providers: false }); await newer })
  await waitFor(() => expect(within(newForm).getByRole('button', { name: 'Submit Analysis', hidden: true })).toBeDisabled())
  expect(within(reForm).getByRole('button', { name: 'Re-Analyze' })).toBeDisabled()
  await waitFor(() => expect(get.mock.calls.filter(([p]) => p.startsWith('/api/ai-models')).length).toBeGreaterThan(1))
  const catalogCalls = get.mock.calls.filter(([p]) => p.startsWith('/api/ai-models')).length
  await act(async () => { resolveOlder({ username: 'alice', role: 'reviewer', is_admin: false, can_view_reports: true, can_use_server_providers: true }); await older })
  expect(within(newForm).getByRole('button', { name: 'Submit Analysis', hidden: true })).toBeDisabled()
  expect(within(reForm).getByRole('button', { name: 'Re-Analyze' })).toBeDisabled()
  expect(get.mock.calls.filter(([p]) => p.startsWith('/api/ai-models'))).toHaveLength(catalogCalls)
})

it('keeps both active analysis forms enabled on focus network failure, then revokes on 401 and recovers', async () => {
  setup()
  granted = true
  const result = { request_params: { ai_provider: 'claude', ai_model: 'sonnet', force_server_credentials: true } } as unknown as AnalysisResult
  const { container, rerender } = render(page(result, false))
  const newForm = within(container.querySelector('section')!)
  const user = userEvent.setup()
  await user.click(await newForm.findByRole('button', { name: 'AI Configuration' }))
  await user.click(newForm.getByRole('button', { name: 'Paste XML' }))
  await user.type(newForm.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
  rerender(page(result, true))
  const reForm = within(await screen.findByRole('dialog'))
  const submit = () => newForm.getByRole('button', { name: 'Submit Analysis', hidden: true })
  const reanalyze = () => reForm.getByRole('button', { name: 'Re-Analyze' })
  await waitFor(() => expect(submit()).toBeEnabled())
  await waitFor(() => expect(reanalyze()).toBeEnabled())

  for (const error of [new Error('offline'), new ApiError(503, 'unavailable', null)]) {
    get.mockImplementation((path: string) => path === '/api/auth/me'
      ? Promise.reject(error)
      : path === '/api/user/tokens' ? Promise.resolve({ github_token: '', jira_email: '', jira_token: '' })
        : path.startsWith('/api/ai-models') ? Promise.resolve({ providers: { claude: [serverModel], openai: [userModel] }, provider_status: {} })
          : Promise.resolve(defaults))
    await act(async () => { window.dispatchEvent(new Event('focus')) })
    expect(submit()).toBeEnabled()
    expect(reanalyze()).toBeEnabled()
  }

  granted = false
  setup()
  await act(async () => { window.dispatchEvent(new Event('focus')) })
  await waitFor(() => expect(submit()).toBeDisabled())
  expect(reanalyze()).toBeDisabled()
  granted = true
  await act(async () => { window.dispatchEvent(new Event('focus')) })
  await waitFor(() => expect(submit()).toBeEnabled())
  expect(reanalyze()).toBeEnabled()
  get.mockImplementation((path: string) => path === '/api/auth/me'
    ? Promise.reject(new ApiError(401, 'unauthorized', null)) : Promise.resolve(defaults))
  await act(async () => { window.dispatchEvent(new Event('focus')) })
  await waitFor(() => expect(submit()).toBeDisabled())
  expect(reanalyze()).toBeDisabled()
})

it('updates mounted re-analysis primary and peer eligibility on visibility grant changes', async () => {
  setup()
  granted = true
  const result = { request_params: { ai_provider: 'claude', ai_model: 'sonnet', force_server_credentials: true, peer_ai_configs: [{ ai_provider: 'claude', ai_model: 'sonnet' }] } } as unknown as AnalysisResult
  render(<AuthProvider><MemoryRouter><ReAnalyzeDialog open onOpenChange={() => {}} result={result} jobId="job" /></MemoryRouter></AuthProvider>)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Re-Analyze' })).toBeEnabled())
  granted = false
  act(() => document.dispatchEvent(new Event('visibilitychange')))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Re-Analyze' })).toBeDisabled())
  await userEvent.setup().click(screen.getByRole('combobox', { name: 'Peer 1 model' }))
  expect(screen.getByRole('option', { name: /sonnet.*Server/i })).toHaveAttribute('aria-disabled', 'true')
  expect(screen.getByRole('switch', { name: 'Use server credentials' })).toBeDisabled()
  granted = true
  act(() => window.dispatchEvent(new Event('focus')))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Re-Analyze' })).toBeEnabled())
  expect(post).not.toHaveBeenCalled()
})
