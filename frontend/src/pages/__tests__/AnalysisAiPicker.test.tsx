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
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ username: 'alice', isAdmin: false, authenticated: true }) }))

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
  poll_interval_minutes: 2, max_wait_minutes: 0,
}

// Radix Select needs pointer capture, which jsdom does not implement.
HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

function setup() {
  get.mockImplementation(async (path: string) => path === '/api/ai-models' ? catalog : defaults)
  post.mockResolvedValue({ job_id: 'next' })
}

afterEach(() => { act(() => resetProviderCatalogCache()); get.mockReset(); post.mockReset() })

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
    expect(screen.getByRole('option', { name: 'Gemini · Server' })).toBeInTheDocument()
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
    await user.click(screen.getByRole('option', { name: 'Anthropic · Server' }))
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
    expect(screen.getByRole('combobox', { name: 'Peer 1 model' })).toHaveValue('suggested (unavailable)')
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
    expect(screen.getByRole('combobox', { name: 'AI Model' })).toHaveValue('old (unavailable)')
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
