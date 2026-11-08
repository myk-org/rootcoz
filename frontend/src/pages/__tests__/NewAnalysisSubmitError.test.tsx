import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { AuthProvider } from '@/lib/AuthProvider'
import { ApiError } from '@/lib/api'
import { NewAnalysisPage } from '@/pages/NewAnalysisPage'
import { resetProviderCatalogCache } from '@/lib/useProviderOptions'

const get = vi.fn()
const post = vi.fn()
vi.mock('@/lib/api', async (importActual) => ({
  ...await importActual<typeof import('@/lib/api')>(),
  api: { get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args) },
}))

const GENERIC = 'Failed to submit analysis. Please try again.'

// A user-credentialed pair so the submit button is enabled without any server grant.
const defaults = {
  ai_provider: 'openai', ai_model: 'gpt',
  ai_call_timeout: 10, tests_repo_url: '', additional_repos: [], peer_ai_configs: [],
  peer_analysis_max_rounds: 3, jira_enabled: false, jira_url: '', jira_project_key: '',
  get_job_artifacts: false, jenkins_artifacts_max_size_mb: 50, wait_for_completion: false,
  poll_interval_minutes: 2, max_wait_minutes: 0,
}

function setup() {
  get.mockImplementation(async (path: string) => {
    if (path === '/api/auth/me') return { username: 'alice', role: 'reviewer', is_admin: false, can_view_reports: true, can_use_server_providers: false }
    if (path === '/api/user/tokens') return { github_token: '', jira_email: '', jira_token: '' }
    if (path.startsWith('/api/ai-models')) return { providers: { openai: [{ id: 'gpt', name: 'GPT', provider: 'openai', credential_sources: ['user'] }] }, provider_status: {} }
    return defaults
  })
}

// Radix requires pointer APIs absent from jsdom.
HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

afterEach(() => { act(() => resetProviderCatalogCache()); get.mockReset(); post.mockReset() })

/** Fill the form with two additional repos both named `shared-infra`, then submit. */
async function submitDuplicateRepos(rejection: unknown) {
  setup()
  post.mockRejectedValue(rejection)
  const user = userEvent.setup()
  render(<AuthProvider><MemoryRouter><NewAnalysisPage /></MemoryRouter></AuthProvider>)
  await user.click(await screen.findByRole('button', { name: 'Paste XML' }))
  await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
  await user.click(await screen.findByRole('button', { name: 'Source Repositories' }))
  for (const i of [1, 2]) {
    await user.click(await screen.findByRole('button', { name: 'Add Repository' }))
    await user.type(screen.getAllByPlaceholderText('Name')[i - 1], 'shared-infra')
    await user.type(screen.getAllByPlaceholderText('URL')[i - 1], `https://github.com/org/shared-infra-${i}`)
  }
  const submit = screen.getByRole('button', { name: 'Submit Analysis' })
  await waitFor(() => expect(submit).toBeEnabled())
  await user.click(submit)
  await waitFor(() => expect(post).toHaveBeenCalled())
}

it('shows the duplicate repo names a 422 validation detail carries', async () => {
  await submitDuplicateRepos(new ApiError(422, 'Unprocessable Entity', {
    detail: [{
      type: 'value_error',
      loc: ['body', 'additional_repos'],
      msg: 'Value error, Duplicate additional repo names: shared-infra',
      ctx: { error: {} },
    }],
  }))
  expect(await screen.findByText('Two additional repos are named `shared-infra` — rename one.')).toBeInTheDocument()
  expect(screen.queryByText(GENERIC)).not.toBeInTheDocument()
})

it('keeps the generic sentence for a server error', async () => {
  await submitDuplicateRepos(new ApiError(500, 'Internal Server Error', 'Internal Server Error'))
  expect(await screen.findByText(GENERIC)).toBeInTheDocument()
})
