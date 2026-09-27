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
    await waitFor(() => expect(picker).toHaveTextContent('Openai · User'))
    await user.click(picker)
    expect(screen.getByRole('option', { name: 'Claude · Server' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Openai · User' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Gemini · User + Server' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Mistral · User' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Anthropic · Server' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /Openai · Server/ })).not.toBeInTheDocument()
  })

  it('keeps a stale selected provider visible without inventing a credential source', async () => {
    get.mockResolvedValue({ providers: { empty: [] }, provider_status: {} })
    const user = userEvent.setup()
    render(<ProviderSelect value="legacy" onChange={() => {}} showCredentialSources />)
    const picker = screen.getByRole('combobox')
    await waitFor(() => expect(picker).toHaveTextContent('Legacy'))
    await user.click(picker)
    expect(screen.getByRole('option', { name: 'Legacy' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Empty' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /Legacy · Server|Empty · Server/ })).not.toBeInTheDocument()
  })
})
