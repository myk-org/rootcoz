import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ProviderSelect } from '@/components/shared/ProviderSelect'
import { resetProviderCatalogCache } from '@/lib/useProviderOptions'

const get = vi.fn()
vi.mock('@/lib/api', () => ({ api: { get: (...args: unknown[]) => get(...args) } }))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ username: 'admin', isAdmin: true, authenticated: true }) }))

HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

afterEach(() => { cleanup(); resetProviderCatalogCache(); get.mockReset() })

describe('server settings provider picker', () => {
  it('labels server-only, user-only, mixed and no-listing keys, including the selected option', async () => {
    get.mockResolvedValue({
      providers: {
        claude: [{ id: 'sonnet', name: 'Sonnet', provider: 'claude', credential_sources: ['server'] }],
        openai: [{ id: 'gpt', name: 'GPT', provider: 'openai', credential_sources: ['user'] }],
        gemini: [{ id: 'shared', name: 'Shared', provider: 'gemini', credential_sources: ['user', 'server'] }],
        mistral: [],
        anthropic: [{ id: 'mixed', name: 'Mixed', provider: 'anthropic', credential_sources: ['user', 'server'], verified: false }],
      },
      provider_status: { mistral: { has_api_key: true, modelListingSupported: false } },
    })
    const user = userEvent.setup()
    render(<ProviderSelect value="openai" onChange={() => {}} showCredentialSources />)
    const picker = screen.getByRole('combobox')
    await waitFor(() => expect(picker).toHaveTextContent('OpenAI · User'))
    await user.click(picker)
    expect(screen.getByRole('option', { name: 'Claude · Server' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'OpenAI · User' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Gemini · User + Server' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Mistral · User' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Anthropic · Server' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /OpenAI · Server/ })).not.toBeInTheDocument()
  })

  it('keeps a stale selected provider visible without inventing a credential source', async () => {
    get.mockResolvedValue({ providers: { empty: [] }, provider_status: {} })
    const user = userEvent.setup()
    render(<ProviderSelect value="legacy" onChange={() => {}} showCredentialSources />)
    const picker = screen.getByRole('combobox')
    await waitFor(() => expect(picker).toHaveTextContent('legacy'))
    await user.click(picker)
    expect(screen.getByRole('option', { name: 'legacy' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Empty' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /legacy · Server|Empty · Server/ })).not.toBeInTheDocument()
  })

  it('renders public labels for sidecar ids and preserves custom provider casing', async () => {
    get.mockResolvedValue({
      providers: {
        'google-vertex-claude': [{ id: 'claude-sonnet-4-6', name: 'Sonnet', provider: 'google-vertex-claude', credential_sources: ['server'] }],
        'google-vertex': [{ id: 'gemini-2.5-pro', name: 'Gemini', provider: 'google-vertex', credential_sources: ['server'] }],
        openai: [{ id: 'gpt-5', name: 'GPT-5', provider: 'openai', credential_sources: ['user'] }],
        MyGateway: [{ id: 'gw-model', name: 'GW', provider: 'MyGateway', credential_sources: ['server'] }],
      },
      provider_status: {},
    })
    const user = userEvent.setup()
    render(<ProviderSelect value="google-vertex-claude" onChange={() => {}} showCredentialSources />)
    const picker = screen.getByRole('combobox')
    await waitFor(() => expect(picker).toHaveTextContent('Claude · Server'))
    await user.click(picker)
    expect(screen.getByRole('option', { name: 'Claude · Server' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Gemini · Server' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'MyGateway · Server' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'OpenAI · User' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /Google Vertex|Openai|Mygateway/ })).not.toBeInTheDocument()
  })

  it('disambiguates google and google-vertex when both exist on server credentials', async () => {
    get.mockResolvedValue({
      providers: {
        google: [{ id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', provider: 'google', credential_sources: ['server'] }],
        'google-vertex': [{ id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', provider: 'google-vertex', credential_sources: ['server'] }],
      },
      provider_status: {},
    })
    const user = userEvent.setup()
    render(<ProviderSelect value="google-vertex" onChange={() => {}} showCredentialSources />)
    const picker = screen.getByRole('combobox')
    await waitFor(() => expect(picker).toHaveTextContent('Gemini (Vertex) · Server'))
    await user.click(picker)
    expect(screen.getByRole('option', { name: 'Gemini (API) · Server' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Gemini (Vertex) · Server' })).toBeInTheDocument()
  })
})
