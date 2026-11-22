import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { NewAnalysisPage } from '@/pages/NewAnalysisPage'
import { AnalysisProviderSelect } from '@/components/shared/AnalysisAiPicker'
import { ReAnalyzeDialog } from '@/pages/report/ReAnalyzeDialog'
import { TooltipProvider } from '@/components/ui/tooltip'
import { resetProviderCatalogCache } from '@/lib/useProviderOptions'
import { writeLastAiPair } from '@/lib/lastAiPair'
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
    openrouter: { modelListingSupported: false, has_api_key: true },
    mistral: { modelListingSupported: false, has_api_key: true },
    anthropic: { modelListingSupported: false, has_api_key: true },
  },
}
const defaults = {
  ai_provider: 'openai', ai_model: 'gpt', ai_call_timeout: 10, tests_repo_url: '', additional_repos: [],
  peer_ai_configs: [], peer_analysis_max_rounds: 3, jira_enabled: false, jira_url: '', jira_project_key: '',
  get_job_artifacts: false, jenkins_artifacts_max_size_mb: 50, wait_for_completion: false,
  poll_interval_minutes: 2, max_wait_minutes: 0,
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

afterEach(() => { grant = true; act(() => resetProviderCatalogCache()); get.mockReset(); post.mockReset(); localStorage.clear() })

describe('analysis credential-scoped pickers', () => {
  it('offers catalog-backed server and own-key sources, and submits the chosen pair', async () => {
    setup()
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await screen.findByRole('button', { name: 'AI Configuration' })
    // The AI section is collapsed on new analysis.
    await user.click(screen.getByRole('button', { name: 'AI Configuration' }))
    const provider = screen.getByRole('combobox', { name: 'AI Provider' })
    await user.click(provider)
    expect(await screen.findByRole('option', { name: 'Claude · Server' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'OpenAI · User' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Gemini · User + Server' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Anthropic · User + Server' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /empty/i })).not.toBeInTheDocument()
    await user.click(screen.getByRole('option', { name: 'Claude · Server' }))
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    expect(screen.getByRole('option', { name: /sonnet.*Server/i })).toBeInTheDocument()
    await user.click(screen.getByRole('option', { name: /sonnet.*Server/i }))
    await user.click(screen.getByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    await user.click(screen.getByRole('button', { name: 'Submit Analysis' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/analyze', expect.objectContaining({ ai_provider: 'claude', ai_model: 'sonnet' })))
    // No credential-source override rides along: the grant alone decides.
    expect(Object.keys(post.mock.calls[0][1]).some((key) => key.includes('credential'))).toBe(false)
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
    await user.click(screen.getByRole('option', { name: 'OpenRouter · User' }))
    const model = screen.getByRole('combobox', { name: 'AI Model' })
    expect(model).not.toHaveAttribute('readonly')
    await user.click(model)
    expect(screen.getByRole('option', { name: /suggested.*UNVERIFIED/i })).toBeInTheDocument()
    // The notice must name the bundled snapshot, not just say "unverified"
    expect(screen.getByText(/bundled suggestions from an older pi-ai catalog/i)).toBeInTheDocument()
    expect(screen.getByText(/Newer models are missing/i)).toBeInTheDocument()
    await user.type(model, 'custom-model')
    await user.click(screen.getByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    await user.click(screen.getByRole('button', { name: 'Submit Analysis' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/analyze', expect.objectContaining({ ai_provider: 'openrouter', ai_model: 'custom-model' })))
    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    await user.click(screen.getByRole('option', { name: 'Mistral · User' }))
    expect(screen.getByRole('combobox', { name: 'AI Model' })).not.toHaveAttribute('readonly')
  })

  it('does not present unverified IDs as available even if a listing-capable provider includes them', async () => {    setup()
    catalog.providers.openai.push({ id: 'not-confirmed', name: 'Not confirmed', provider: 'openai', credential_sources: ['user'], verified: false })
    const user = userEvent.setup()
    const result = { request_params: { ai_provider: 'openai', ai_model: 'not-confirmed', peer_ai_configs: [] } } as unknown as AnalysisResult
    render(<MemoryRouter><ReAnalyzeDialog open onOpenChange={() => {}} result={result} jobId="job" /></MemoryRouter>)
    expect(await screen.findByRole('button', { name: 'Re-Analyze' })).toBeDisabled()
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    expect(screen.queryByRole('option', { name: /not-confirmed/i })).not.toBeInTheDocument()
    catalog.providers.openai.pop()
  })

  it('keeps stored unavailable choices visible but unselectable and submits the chosen pair on reanalysis', async () => {
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
    await user.click(screen.getByRole('option', { name: 'OpenAI · User' }))
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    const model = within(document.body).getByRole('option', { name: /gpt.*User/i })
    await user.click(model)
    await user.click(screen.getByRole('button', { name: 'Re-Analyze' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/re-analyze/job', expect.objectContaining({ ai_provider: 'openai', ai_model: 'gpt' })))
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
    await waitFor(() => expect(post).toHaveBeenCalledWith('/re-analyze/job', expect.objectContaining({ ai_provider: 'openrouter', ai_model: 'suggested', peer_ai_configs: [{ ai_provider: 'mistral', ai_model: 'manual-peer' }] })))
  })

  it('mutes only unavailable server sources, keeps own-key models usable, and blocks stale server reanalysis and peers', async () => {
    setup()
    grant = false
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'AI Configuration' }))
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
    await waitFor(() => expect(post).toHaveBeenCalledWith('/analyze', expect.objectContaining({ ai_provider: 'gemini', ai_model: 'shared' })))
    await user.click(screen.getByRole('button', { name: 'Peer Analysis' }))
    await user.click(screen.getByRole('switch', { name: 'Enable peer review' }))
    const peerTrigger = screen.getByRole('combobox', { name: 'Peer 1 provider' })
    expect(peerTrigger).toHaveAccessibleDescription('Organization access is unavailable. Ask an admin for access.')
    await user.click(peerTrigger)
    expect(screen.getAllByRole('option', { name: /Claude · Server$/ })[0]).toHaveAttribute('data-disabled')
    const peerMixed = screen.getByRole('option', { name: /Gemini · User \+ Server$/ })
    expect(within(peerMixed).getByText('Server')).toHaveClass('text-text-tertiary')
    const result = { request_params: { ai_provider: 'claude', ai_model: 'sonnet', peer_ai_configs: [] } } as unknown as AnalysisResult
    render(<MemoryRouter><ReAnalyzeDialog open onOpenChange={() => {}} result={result} jobId="job" /></MemoryRouter>)
    expect(await screen.findByRole('button', { name: 'Re-Analyze' })).toBeDisabled()
  })

  it('uses own-key choices for a saved server job after access is revoked', async () => {
    setup()
    grant = false
    const user = userEvent.setup()
    const result = { request_params: { ai_provider: 'gemini', ai_model: 'shared', peer_ai_configs: [{ ai_provider: 'openai', ai_model: 'gpt' }] } } as unknown as AnalysisResult
    render(<MemoryRouter><ReAnalyzeDialog open onOpenChange={() => {}} result={result} jobId="job" /></MemoryRouter>)
    const submit = await screen.findByRole('button', { name: 'Re-Analyze' })
    await waitFor(() => expect(submit).toBeEnabled())
    expect(screen.queryByRole('switch', { name: 'Use server credentials' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    expect(screen.getByRole('option', { name: /user-extra.*User/i })).not.toHaveAttribute('aria-disabled', 'true')
    await user.click(screen.getByRole('option', { name: /user-extra.*User/i }))
    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    expect(screen.getByRole('option', { name: /Claude · Server$/ })).toHaveAttribute('data-disabled')
    await user.keyboard('{Escape}')
    expect(submit).toBeEnabled()
    expect(screen.getByRole('combobox', { name: 'Peer 1 provider', hidden: true })).toBeInTheDocument()
  })

  it('blocks a saved server job with no own key after access is revoked', async () => {
    grant = false
    get.mockImplementation(async (path: string) => path === '/api/ai-models' ? { providers: { claude: catalog.providers.claude }, provider_status: {} } : defaults)
    const result = { request_params: { ai_provider: 'claude', ai_model: 'sonnet', peer_ai_configs: [] } } as unknown as AnalysisResult
    render(<MemoryRouter><ReAnalyzeDialog open onOpenChange={() => {}} result={result} jobId="job" /></MemoryRouter>)
    expect(await screen.findByRole('button', { name: 'Re-Analyze' })).toBeDisabled()
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

describe('new analysis stored default (#591)', () => {
  // A teardown refetch (the reset above runs before the page unmounts) leaves an
  // in-flight catalog promise behind; clear it so this mount really asks the server.
  beforeEach(() => act(() => resetProviderCatalogCache()))

  /** The stored server default this mount reads, as the deployment had it: a provider the catalog lost. */
  const unserved = { ai_provider: 'removed-provider', ai_model: 'removed-model' }
  /** Every path the API client was asked for — a listing for an absent provider is the bug. */
  const requestedPaths = () => get.mock.calls.map(([path]) => String(path))

  it('treats an unserved stored default as no selection and requests nothing for it', async () => {
    get.mockImplementation(async (path: string) => path.startsWith('/api/ai-models') ? catalog : { ...defaults, ...unserved })
    post.mockResolvedValue({ job_id: 'next' })
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'AI Configuration' }))
    const provider = screen.getByRole('combobox', { name: 'AI Provider' })
    // No selection at all: not the unserved provider behind an "(unavailable)" trigger.
    await waitFor(() => expect(provider).toHaveTextContent('Select provider...'))
    expect(provider).not.toHaveTextContent(/removed-provider/i)
    // The catalog that answers has no model listing to ask for: no per-provider request.
    expect(requestedPaths().some((path) => path.includes('provider='))).toBe(false)
    // Sending is refused client-side, with the actionable message, instead of a 400 or
    // a silent submit of the server's own (equally unserved) default.
    await user.click(screen.getByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    expect(screen.getByRole('button', { name: 'Submit Analysis' })).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent(/Select an available AI provider and model/)
    expect(post).not.toHaveBeenCalled()
    // The picker offers the providers the catalog does have.
    await user.click(provider)
    expect(await screen.findByRole('option', { name: 'Claude · Server' })).toBeInTheDocument()
  })

  it('applies and submits a stored default the catalog serves, unchanged', async () => {
    setup()
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'AI Configuration' }))
    expect(await screen.findByRole('combobox', { name: 'AI Model' })).toHaveValue('gpt')
    expect(screen.getByRole('combobox', { name: 'AI Provider' })).not.toHaveTextContent(/unavailable/i)
    await user.click(screen.getByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    await user.click(screen.getByRole('button', { name: 'Submit Analysis' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/analyze', expect.objectContaining({ ai_provider: 'openai', ai_model: 'gpt' })))
  })

  it('submits the server default when the remembered pair is the empty one', async () => {
    setup()
    // "Server default" picked on another surface is remembered as an empty pair: no
    // override, not "no selection" — the form runs the server default with no tests repo.
    writeLastAiPair('alice', { provider: '', model: '' })
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'AI Configuration' }))
    // What will run is what the picker shows: the pair the server default resolves to.
    expect(await screen.findByRole('combobox', { name: 'AI Model' })).toHaveValue('gpt')
    await user.click(screen.getByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    const submit = screen.getByRole('button', { name: 'Submit Analysis' })
    expect(submit).toBeEnabled()
    await user.click(submit)
    await waitFor(() => expect(post).toHaveBeenCalledWith('/analyze', expect.objectContaining({ ai_provider: 'openai', ai_model: 'gpt' })))
  })

  it('lets a pick from the catalog replace an unserved stored default', async () => {
    get.mockImplementation(async (path: string) => path.startsWith('/api/ai-models') ? catalog : { ...defaults, ...unserved })
    post.mockResolvedValue({ job_id: 'next' })
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'AI Configuration' }))
    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    await user.click(await screen.findByRole('option', { name: 'Claude · Server' }))
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    await user.click(await screen.findByRole('option', { name: /sonnet.*Server/i }))
    await user.click(screen.getByRole('button', { name: 'Paste XML' }))
    await user.type(screen.getByPlaceholderText('Paste JUnit XML content...'), '<testsuite/>')
    await user.click(screen.getByRole('button', { name: 'Submit Analysis' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/analyze', expect.objectContaining({ ai_provider: 'claude', ai_model: 'sonnet' })))
    // Nothing is written back: the stored default stays the admin's to change.
    expect(post.mock.calls.map(([path]) => path)).toEqual(['/analyze'])
  })

  it('maps internal sidecar ids to public labels and preserves custom provider casing in pickers', async () => {
    const sidecarCatalog: AiModelsResponse = {
      providers: {
        'google-vertex-claude': [{ id: 'claude-sonnet-4-6', name: 'Claude Sonnet 4.6', provider: 'google-vertex-claude', credential_sources: ['server'] }],
        'google-vertex': [{ id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', provider: 'google-vertex', credential_sources: ['server'] }],
        openai: [{ id: 'gpt-5', name: 'GPT-5', provider: 'openai', credential_sources: ['user'] }],
        MyGateway: [{ id: 'gw/model-1', name: 'Model 1', provider: 'MyGateway', credential_sources: ['server'] }],
      },
      provider_status: {},
    }
    get.mockImplementation(async (path: string) => path.startsWith('/api/ai-models') ? sidecarCatalog : { ...defaults, ai_provider: 'google-vertex-claude', ai_model: 'claude-sonnet-4-6' })
    const user = userEvent.setup()
    render(<MemoryRouter><NewAnalysisPage /></MemoryRouter>)
    await user.click(await screen.findByRole('button', { name: 'AI Configuration' }))
    const provider = screen.getByRole('combobox', { name: 'AI Provider' })
    expect(provider).toHaveTextContent('Claude · Server')
    await user.click(provider)
    expect(screen.getByRole('option', { name: 'Claude · Server' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Gemini · Server' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'MyGateway · Server' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'OpenAI · User' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /Google Vertex|Openai|Mygateway/ })).not.toBeInTheDocument()
  })

  it('preserves transport disambiguation across forceServer filtering and unavailable placeholders', async () => {
    const dualGeminiCatalog: AiModelsResponse = {
      providers: {
        google: [{ id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', provider: 'google', credential_sources: ['user'] }],
        'google-vertex': [{ id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', provider: 'google-vertex', credential_sources: ['server'] }],
      },
      provider_status: {},
    }
    get.mockImplementation(async () => dualGeminiCatalog)
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <TooltipProvider>
        <AnalysisProviderSelect value="google" onChange={onChange} forceServer={true} />
      </TooltipProvider>,
    )
    const trigger = await screen.findByRole('combobox', { name: 'AI Provider' })
    // google is filtered out by forceServer=true, so the placeholder must stay
    // collision-aware ("Gemini (API) (unavailable)"), not ambiguous "Gemini (unavailable)".
    await waitFor(() => expect(trigger).toHaveTextContent('Gemini (API) (unavailable)'))
    await user.click(trigger)
    // The selectable server option must also keep its full-catalog qualifier so
    // it agrees with ChatUI/Report headers and stays stable across forceServer toggles.
    expect(await screen.findByRole('option', { name: 'Gemini (Vertex) · Server' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'Gemini · Server' })).not.toBeInTheDocument()
  })
})
