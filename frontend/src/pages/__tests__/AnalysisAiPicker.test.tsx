import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { NewAnalysisPage } from '@/pages/NewAnalysisPage'
import { PeerConfigList } from '@/components/shared/PeerConfigList'
import { ReAnalyzeDialog } from '@/pages/report/ReAnalyzeDialog'
import { resetProviderCatalogCache } from '@/lib/useProviderOptions'
import type { AnalysisResult, AiModelsResponse } from '@/types'

const get = vi.fn()
const post = vi.fn()
vi.mock('@/lib/api', () => ({ api: { get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args) } }))
let grant = true
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ username: 'alice', isAdmin: false, authenticated: true, canUseServerProviders: grant }) }))

const catalog: AiModelsResponse = {
  providers: {
    claude: [{ id: 'sonnet', name: 'Sonnet', provider: 'claude', credential_sources: ['server'] }],
    openai: [{ id: 'gpt', name: 'GPT', provider: 'openai', credential_sources: ['user'] }],
    gemini: [{ id: 'shared', name: 'Shared', provider: 'gemini', credential_sources: ['user', 'server'] }, { id: 'user-extra', name: 'User extra', provider: 'gemini', credential_sources: ['user'] }],
    empty: [],
    openrouter: [{ id: 'suggested', name: 'Suggested', provider: 'openrouter', credential_sources: ['user'], verified: false }],
    mistral: [],
    anthropic: [{ id: 'server-model', name: 'Server model', provider: 'anthropic', credential_sources: ['server'], verified: true }, { id: 'mixed', name: 'Mixed', provider: 'anthropic', credential_sources: ['user', 'server'], verified: false }],
  },
  provider_status: {
    openrouter: { ok: true, modelListingSupported: false, has_api_key: true },
    mistral: { ok: true, modelListingSupported: false, has_api_key: true },
    anthropic: { modelListingSupported: false, has_api_key: true },
  },
}
const defaults = {
  ai_provider: 'openai', ai_model: 'gpt', ai_call_timeout: 10, tests_repo_url: '', additional_repos: [],
  peer_ai_configs: [], peer_analysis_max_rounds: 3, jira_enabled: false, jira_url: '', jira_project_key: '',
  get_job_artifacts: false, jenkins_artifacts_max_size_mb: 50, wait_for_completion: false,
  poll_interval_minutes: 2, max_wait_minutes: 0, force_server_credentials: false,
}

// Radix Select needs pointer capture, which jsdom does not implement.
HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

function setup() {
  get.mockImplementation(async (path: string) => path.startsWith('/api/ai-models') ? catalog : defaults)
  post.mockResolvedValue({ job_id: 'next' })
}

afterEach(() => { grant = true; act(() => resetProviderCatalogCache()); get.mockReset(); post.mockReset() })

describe('analysis credential-scoped pickers', () => {
  it('shows only catalog-backed sources, and submits explicit server override on new analysis', async () => {
    setup()
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await screen.findByRole('button', { name: 'AI Configuration' })
    // The AI section is collapsed on new analysis.
    await user.click(screen.getByRole('button', { name: 'AI Configuration' }))
    await screen.findByRole('switch', { name: 'Use server credentials' })
    const provider = screen.getByRole('combobox', { name: 'AI Provider' })
    await user.click(provider)
    expect(await screen.findByRole('option', { name: 'Claude · Server' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Openai · User' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Gemini · User + Server' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Anthropic · User + Server' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /empty/i })).not.toBeInTheDocument()
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('switch', { name: 'Use server credentials' }))
    await user.click(provider)
    expect(screen.queryByRole('option', { name: /Openai/ })).not.toBeInTheDocument()
    expect(await screen.findByRole('option', { name: 'Gemini · Server' })).toBeInTheDocument()
    await user.click(screen.getByRole('option', { name: 'Gemini · Server' }))
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    expect(screen.queryByRole('option', { name: /user-extra/i })).not.toBeInTheDocument()
    expect(screen.getByRole('option', { name: /shared.*Server/i })).toBeInTheDocument()
    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    await user.click(screen.getByRole('option', { name: 'Claude · Server' }))
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    expect(screen.getByRole('option', { name: /sonnet.*Server/i })).toBeInTheDocument()
    await user.click(screen.getByRole('option', { name: /sonnet.*Server/i }))
    await user.click(screen.getByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    await user.click(screen.getByRole('button', { name: 'Submit Analysis' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/analyze', expect.objectContaining({ ai_provider: 'claude', ai_model: 'sonnet', force_server_credentials: true })))
  })

  it('keeps a mixed-source model on forced server even when user listing is unverified', async () => {
    setup()
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'AI Configuration' }))
    await user.click(screen.getByRole('switch', { name: 'Use server credentials' }))
    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    await user.click(await screen.findByRole('option', { name: 'Anthropic · Server' }))
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    await user.click(screen.getByRole('option', { name: /mixed.*Server/i }))
    await user.click(screen.getByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    await user.click(screen.getByRole('button', { name: 'Submit Analysis' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/analyze', expect.objectContaining({ ai_provider: 'anthropic', ai_model: 'mixed', force_server_credentials: true })))
  })

  it('requires a model after selecting a provider on new analysis', async () => {
    setup()
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'AI Configuration' }))
    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    await user.click(screen.getByRole('option', { name: 'Claude · Server' }))
    await user.click(screen.getByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    expect(screen.getByRole('button', { name: 'Submit Analysis' })).toBeDisabled()
    expect(post).not.toHaveBeenCalled()
  })

  it('offers unverified suggestions and manual IDs only for user keys without model listing', async () => {
    setup()
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'AI Configuration' }))
    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    expect(await screen.findByRole('option', { name: 'Mistral · User' })).toBeInTheDocument()
    await user.click(screen.getByRole('option', { name: 'Openrouter · User' }))
    const model = screen.getByRole('combobox', { name: 'AI Model' })
    expect(model).not.toHaveAttribute('readonly')
    await user.click(model)
    expect(screen.getByRole('option', { name: /suggested.*UNVERIFIED/i })).toBeInTheDocument()
    expect(screen.getByText(/not verified.*model ID/i)).toBeInTheDocument()
    await user.type(model, 'custom-model')
    await user.click(screen.getByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    await user.click(screen.getByRole('button', { name: 'Submit Analysis' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/analyze', expect.objectContaining({ ai_provider: 'openrouter', ai_model: 'custom-model', force_server_credentials: false })))
    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    await user.click(screen.getByRole('option', { name: 'Mistral · User' }))
    expect(screen.getByRole('combobox', { name: 'AI Model' })).not.toHaveAttribute('readonly')
    await user.click(screen.getByRole('switch', { name: 'Use server credentials' }))
    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    expect(screen.queryByRole('option', { name: /Mistral|Openrouter/ })).not.toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.getByRole('button', { name: 'Submit Analysis' })).toBeDisabled()
  })

  it('does not present unverified IDs as available even if a listing-capable provider includes them', async () => {
    setup()
    catalog.providers.openai.push({ id: 'not-confirmed', name: 'Not confirmed', provider: 'openai', credential_sources: ['user'], verified: false })
    const user = userEvent.setup()
    const result = { request_params: { ai_provider: 'openai', ai_model: 'not-confirmed', peer_ai_configs: [] } } as unknown as AnalysisResult
    render(<MemoryRouter><ReAnalyzeDialog open onOpenChange={() => {}} result={result} jobId="job" /></MemoryRouter>)
    expect(await screen.findByRole('button', { name: 'Re-Analyze' })).toBeDisabled()
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    expect(screen.queryByRole('option', { name: /not-confirmed/i })).not.toBeInTheDocument()
    catalog.providers.openai.pop()
  })

  it('restricts peer provider and model to the selected credential source', async () => {
    setup()
    const user = userEvent.setup()
    render(<MemoryRouter><PeerConfigList peerConfigs={[{ id: 'peer', ai_provider: 'openrouter', ai_model: 'suggested' }]} setPeerConfigs={() => {}} peerModels={{}} maxRounds={1} setMaxRounds={() => {}} strict forceServer /></MemoryRouter>)
    const provider = await screen.findByRole('combobox', { name: 'Peer 1 provider' })
    expect(provider).toHaveTextContent('openrouter (unavailable)')
    // The model input stays a real combobox: raw value, never decorated, so the
    // user can edit it. Unavailability is conveyed by the disabled list option.
    const model = screen.getByRole('combobox', { name: 'Peer 1 model' })
    expect(model).toHaveValue('suggested')
    expect(model).not.toHaveAttribute('readonly')
    await user.click(provider)
    expect(screen.queryByRole('option', { name: /Openrouter|Openai/ })).not.toBeInTheDocument()
  })

  it('keeps stored unavailable choices visible but unselectable and sends false on reanalysis', async () => {
    setup()
    const user = userEvent.setup()
    const result = { request_params: { ai_provider: 'missing', ai_model: 'old', peer_ai_configs: [] } } as unknown as AnalysisResult
    render(<MemoryRouter><ReAnalyzeDialog open onOpenChange={() => {}} result={result} jobId="job" /></MemoryRouter>)
    const provider = await screen.findByRole('combobox', { name: 'AI Provider' })
    expect(provider).toHaveTextContent('missing (unavailable)')
    const modelInput = screen.getByRole('combobox', { name: 'AI Model' })
    expect(modelInput).toHaveValue('old')
    expect(modelInput).not.toHaveAttribute('readonly')
    expect(screen.getByRole('button', { name: 'Re-Analyze' })).toBeDisabled()
    await user.click(provider)
    expect(screen.queryByRole('option', { name: /missing/i })).not.toBeInTheDocument()
    await user.click(screen.getByRole('option', { name: 'Openai · User' }))
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    const model = within(document.body).getByRole('option', { name: /gpt.*User/i })
    await user.click(model)
    await user.click(screen.getByRole('button', { name: 'Re-Analyze' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/re-analyze/job', expect.objectContaining({ ai_provider: 'openai', ai_model: 'gpt', force_server_credentials: false })))
  })

  it('requires peer selections on new analysis too', async () => {
    setup()
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    await user.click(screen.getByRole('button', { name: 'Peer Analysis' }))
    await user.click(screen.getByRole('switch', { name: 'Enable peer review' }))
    expect(screen.getByRole('button', { name: 'Submit Analysis' })).toBeDisabled()
    expect(post).not.toHaveBeenCalled()
  })

  it('requires a model after changing providers on reanalysis', async () => {
    setup()
    const user = userEvent.setup()
    const result = { request_params: { ai_provider: 'openai', ai_model: 'gpt', peer_ai_configs: [] } } as unknown as AnalysisResult
    render(<MemoryRouter><ReAnalyzeDialog open onOpenChange={() => {}} result={result} jobId="job" /></MemoryRouter>)
    await user.click(await screen.findByRole('combobox', { name: 'AI Provider' }))
    await user.click(screen.getByRole('option', { name: 'Claude · Server' }))
    expect(screen.getByRole('button', { name: 'Re-Analyze' })).toBeDisabled()
    expect(post).not.toHaveBeenCalled()
  })

  it('requires a peer provider and model when peer review is enabled', async () => {
    setup()
    const user = userEvent.setup()
    const result = { request_params: { ai_provider: 'openai', ai_model: 'gpt', peer_ai_configs: [] } } as unknown as AnalysisResult
    render(<MemoryRouter><ReAnalyzeDialog open onOpenChange={() => {}} result={result} jobId="job" /></MemoryRouter>)
    await screen.findByRole('button', { name: 'Re-Analyze' })
    await user.click(screen.getByRole('button', { name: 'Peer Analysis' }))
    await user.click(screen.getByRole('switch', { name: 'Enable peer review' }))
    expect(screen.getByRole('button', { name: 'Re-Analyze' })).toBeDisabled()
    await user.click(screen.getByRole('combobox', { name: 'Peer 1 provider' }))
    await user.click(screen.getByRole('option', { name: 'Claude · Server' }))
    expect(screen.getByRole('button', { name: 'Re-Analyze' })).toBeDisabled()
    expect(post).not.toHaveBeenCalled()
  })

  it('submits unverified manual model IDs and peer configs on reanalysis', async () => {
    setup()
    const user = userEvent.setup()
    const result = { request_params: { ai_provider: 'openrouter', ai_model: 'suggested', peer_ai_configs: [{ ai_provider: 'mistral', ai_model: 'manual-peer' }] } } as unknown as AnalysisResult
    render(<MemoryRouter><ReAnalyzeDialog open onOpenChange={() => {}} result={result} jobId="job" /></MemoryRouter>)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Re-Analyze' })).not.toBeDisabled())
    expect(screen.getByRole('combobox', { name: 'Peer 1 model' })).toHaveValue('manual-peer')
    await user.click(screen.getByRole('button', { name: 'Re-Analyze' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/re-analyze/job', expect.objectContaining({ ai_provider: 'openrouter', ai_model: 'suggested', force_server_credentials: false, peer_ai_configs: [{ ai_provider: 'mistral', ai_model: 'manual-peer' }] })))
  })

  it('mutes only unavailable server sources, keeps own-key models usable, and blocks stale server reanalysis and peers', async () => {
    setup()
    grant = false
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'AI Configuration' }))
    expect(screen.getByRole('switch', { name: 'Use server credentials' })).toBeDisabled()
    const providerTrigger = screen.getByRole('combobox', { name: 'AI Provider' })
    for (let i = 0; i < 10 && document.activeElement !== providerTrigger; i++) await user.tab()
    expect(providerTrigger).toHaveFocus()
    expect(providerTrigger).toHaveAccessibleDescription('Organization access is unavailable. Ask an admin for access.')
    await user.keyboard('{ArrowDown}')
    const serverProvider = screen.getByRole('option', { name: /Claude · Server$/ })
    expect(serverProvider).toHaveAttribute('data-disabled')
    expect(serverProvider).not.toHaveTextContent('locked')
    expect(serverProvider.parentElement).not.toHaveAttribute('tabindex')
    await user.keyboard('{ArrowDown}')
    expect(serverProvider).not.toHaveFocus()
    await user.hover(serverProvider)
    expect(await screen.findByRole('tooltip')).toHaveTextContent('Organization access is unavailable. Ask an admin for access.')
    const mixedProvider = screen.getByRole('option', { name: /Gemini · User \+ Server$/ })
    expect(mixedProvider).not.toHaveAttribute('data-disabled')
    expect(within(mixedProvider).getByText('Server')).toHaveClass('text-text-tertiary')
    expect(mixedProvider).toHaveTextContent('User + Server')
    await user.hover(mixedProvider)
    expect(await screen.findByRole('tooltip')).toHaveTextContent('Organization access is unavailable. Ask an admin for access.')
    await user.click(mixedProvider)
    expect(screen.getByRole('combobox', { name: 'AI Provider' })).toHaveTextContent('Gemini · User + Server')
    expect(within(screen.getByRole('combobox', { name: 'AI Provider' })).getByText('Server')).toHaveClass('text-text-tertiary')
    const selectedProvider = screen.getByRole('combobox', { name: 'AI Provider' })
    expect(selectedProvider).toHaveAccessibleDescription('Organization access is unavailable. Ask an admin for access.')
    for (let i = 0; i < 20 && document.activeElement !== selectedProvider; i++) await user.tab()
    expect(selectedProvider).toHaveFocus()
    expect(await screen.findByRole('tooltip')).toHaveTextContent('Organization access is unavailable. Ask an admin for access.')
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    const shared = screen.getByRole('option', { name: /shared.*User \+ Server/ })
    expect(within(shared).getByText('Server')).toHaveClass('text-text-tertiary')
    expect(shared).not.toHaveAttribute('aria-disabled', 'true')
    await user.click(shared)
    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    await user.click(screen.getByRole('option', { name: /Anthropic · User \+ Server$/ }))
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    const serverOnly = screen.getByRole('option', { name: /server-model.*Server/ })
    expect(serverOnly).toHaveAttribute('aria-disabled', 'true')
    await user.click(serverOnly)
    expect(screen.getByRole('combobox', { name: 'AI Model' })).toHaveValue('')
    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    await user.click(screen.getByRole('option', { name: /Gemini · User \+ Server$/ }))
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    await user.click(screen.getByRole('option', { name: /shared.*User \+ Server/ }))
    await user.click(screen.getByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    await user.click(screen.getByRole('button', { name: 'Submit Analysis' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/analyze', expect.objectContaining({ ai_provider: 'gemini', ai_model: 'shared', force_server_credentials: false })))
    await user.click(screen.getByRole('button', { name: 'Peer Analysis' }))
    await user.click(screen.getByRole('switch', { name: 'Enable peer review' }))
    const peerTrigger = screen.getByRole('combobox', { name: 'Peer 1 provider' })
    expect(peerTrigger).toHaveAccessibleDescription('Organization access is unavailable. Ask an admin for access.')
    await user.click(peerTrigger)
    expect(screen.getAllByRole('option', { name: /Claude · Server$/ })[0]).toHaveAttribute('data-disabled')
    const peerMixed = screen.getByRole('option', { name: /Gemini · User \+ Server$/ })
    expect(within(peerMixed).getByText('Server')).toHaveClass('text-text-tertiary')
    const result = { request_params: { ai_provider: 'claude', ai_model: 'sonnet', force_server_credentials: true, peer_ai_configs: [] } } as unknown as AnalysisResult
    render(<MemoryRouter><ReAnalyzeDialog open onOpenChange={() => {}} result={result} jobId="job" /></MemoryRouter>)
    expect(await screen.findByRole('button', { name: 'Re-Analyze' })).toBeDisabled()
  })

  it('uses own-key choices for a saved server-forced job after access is revoked', async () => {
    setup()
    grant = false
    const user = userEvent.setup()
    const result = { request_params: { ai_provider: 'gemini', ai_model: 'shared', force_server_credentials: true, peer_ai_configs: [{ ai_provider: 'openai', ai_model: 'gpt' }] } } as unknown as AnalysisResult
    render(<MemoryRouter><ReAnalyzeDialog open onOpenChange={() => {}} result={result} jobId="job" /></MemoryRouter>)
    const submit = await screen.findByRole('button', { name: 'Re-Analyze' })
    await waitFor(() => expect(submit).toBeEnabled())
    expect(screen.getByRole('switch', { name: 'Use server credentials' })).toBeDisabled()
    expect(screen.getByRole('switch', { name: 'Use server credentials' })).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByText(/original analysis used server credentials.*no longer have access/i)).toBeInTheDocument()
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    expect(screen.getByRole('option', { name: /user-extra.*User/i })).not.toHaveAttribute('aria-disabled', 'true')
    await user.click(screen.getByRole('option', { name: /user-extra.*User/i }))
    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    expect(screen.getByRole('option', { name: /Claude · Server$/ })).toHaveAttribute('data-disabled')
    await user.keyboard('{Escape}')
    expect(submit).toBeEnabled()
    expect(screen.getByRole('combobox', { name: 'Peer 1 provider', hidden: true })).toBeInTheDocument()
  })

  it('blocks a saved server-forced job with no own key after access is revoked', async () => {
    grant = false
    get.mockImplementation(async (path: string) => path === '/api/ai-models' ? { providers: { claude: catalog.providers.claude }, provider_status: {} } : defaults)
    const result = { request_params: { ai_provider: 'claude', ai_model: 'sonnet', force_server_credentials: true, peer_ai_configs: [] } } as unknown as AnalysisResult
    render(<MemoryRouter><ReAnalyzeDialog open onOpenChange={() => {}} result={result} jobId="job" /></MemoryRouter>)
    expect(await screen.findByRole('button', { name: 'Re-Analyze' })).toBeDisabled()
    expect(screen.getByText(/original analysis used server credentials.*no longer have access/i)).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(/Add your own key or ask an admin/)
    expect(post).not.toHaveBeenCalled()
  })

  it('blocks analysis with no own key or grant and explains why', async () => {
    grant = false
    get.mockImplementation(async (path: string) => path === '/api/ai-models' ? { providers: { claude: catalog.providers.claude } } : { ...defaults, ai_provider: 'claude', ai_model: 'sonnet' })
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    expect(screen.getByRole('button', { name: 'Submit Analysis' })).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent(/Add your own key or ask an admin/)
    expect(post).not.toHaveBeenCalled()
  })

  it('submits with repository AI settings deferred when both primary fields are empty', async () => {
    get.mockImplementation(async (path: string) => path.startsWith('/api/ai-models') ? { providers: {} } : { ...defaults, ai_provider: '', ai_model: '', tests_repo_url: 'https://github.com/org/tests' })
    post.mockResolvedValue({ job_id: 'next' })
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    await user.click(screen.getByRole('button', { name: 'Submit Analysis' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/analyze', expect.objectContaining({ tests_repo_url: 'https://github.com/org/tests' })))
    const body = post.mock.calls[0][1]
    expect(body).not.toHaveProperty('ai_provider')
    expect(body).not.toHaveProperty('ai_model')
    await user.click(screen.getByRole('button', { name: 'Peer Analysis' }))
    await user.click(screen.getByRole('switch', { name: 'Enable peer review' }))
    expect(screen.getByRole('button', { name: 'Submit Analysis' })).toBeDisabled()
  })

  it('honors the server key default for picker, peers, and submission', async () => {
    get.mockImplementation(async (path: string) => path === '/api/ai-models?force_server_credentials=true'
      ? { providers: { claude: catalog.providers.claude }, provider_status: {} }
      : path === '/api/ai-models' ? { providers: {}, provider_status: {} }
      : { ...defaults, ai_provider: 'claude', ai_model: 'sonnet', force_server_credentials: true })
    post.mockResolvedValue({ job_id: 'next' })
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'AI Configuration' }))
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Use server credentials' })).toHaveAttribute('aria-checked', 'true'))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Submit Analysis' })).toBeDisabled())
    await user.click(screen.getByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Submit Analysis' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Submit Analysis' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/analyze', expect.objectContaining({ ai_provider: 'claude', ai_model: 'sonnet', force_server_credentials: true })))
    expect(get).toHaveBeenCalledWith('/api/ai-models?force_server_credentials=true')
  })

  it('allows an explicit user-key override of a true server default', async () => {
    setup()
    get.mockImplementation(async (path: string) => path.startsWith('/api/ai-models') ? catalog : { ...defaults, ai_provider: 'gemini', ai_model: 'shared', force_server_credentials: true })
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'AI Configuration' }))
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Use server credentials' })).toHaveAttribute('aria-checked', 'true'))
    await user.click(screen.getByRole('switch', { name: 'Use server credentials' }))
    await user.click(screen.getByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Submit Analysis' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Submit Analysis' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/analyze', expect.objectContaining({ force_server_credentials: false })))
  })

  it('recovers server options when the user-key catalog fails', async () => {
    get.mockImplementation(async (path: string) => path === '/api/ai-models'
      ? Promise.reject(new Error('bad user key'))
      : path === '/api/ai-models?force_server_credentials=true'
        ? { providers: { claude: catalog.providers.claude }, provider_status: {} }
        : { ...defaults, ai_provider: 'claude', ai_model: 'sonnet' })
    post.mockResolvedValue({ job_id: 'next' })
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'AI Configuration' }))
    await user.click(screen.getByRole('switch', { name: 'Use server credentials' }))
    await user.click(screen.getByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Submit Analysis' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Submit Analysis' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/analyze', expect.objectContaining({ force_server_credentials: true })))
  })

  it('requires a valid selection when server defaults and catalog are empty', async () => {
    get.mockImplementation(async (path: string) => path === '/api/ai-models' ? { providers: {}, provider_status: {} } : { ...defaults, ai_provider: '', ai_model: '' })
    post.mockResolvedValue({ job_id: 'next' })
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    expect(screen.getByRole('button', { name: 'Submit Analysis' })).toBeDisabled()
    expect(post).not.toHaveBeenCalled()
  })
})
