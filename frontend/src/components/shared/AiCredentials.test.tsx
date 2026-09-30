import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { api } from '@/lib/api'
import { AiCredentials } from './AiCredentials'

Element.prototype.scrollIntoView = vi.fn()

vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), put: vi.fn(), delete: vi.fn() } }))
const catalogMocks = vi.hoisted(() => ({
  providerStatus: {} as Record<string, { has_api_key?: boolean; model_count?: number }>,
}))

vi.mock('@/lib/useProviderOptions', () => ({
  resetProviderCatalogCache: vi.fn(),
  useProviderCatalog: () => ({ providers: {
    'openai/custom': [{ id: 'catalog-model', name: 'Catalog model' }],
    other: [],
  }, providerStatus: catalogMocks.providerStatus }),
}))

const get = vi.mocked(api.get)
const put = vi.mocked(api.put)
const remove = vi.mocked(api.delete)

beforeEach(() => {
  vi.resetAllMocks()
  catalogMocks.providerStatus = {}
  get.mockResolvedValue({ providers: [{ provider: 'openai/custom', configured: false }, { provider: 'other', configured: false }] })
  put.mockResolvedValue({ outcome: 'accepted', ok: true })
  remove.mockResolvedValue(undefined)
})

describe('AiCredentials', () => {
  it('discovers providers, searches, adds two keys, and removes just one', async () => {
    const user = userEvent.setup()
    render(<AiCredentials />)
    const picker = await screen.findByRole('combobox', { name: 'AI provider' })
    expect(screen.queryByText('anthropic')).not.toBeInTheDocument()
    await user.type(picker, 'custom')
    await user.click(await screen.findByRole('option', { name: 'openai/custom' }))
    await user.click(screen.getByRole('combobox', { name: 'Verification model' }))
    await user.click(await screen.findByRole('option', { name: /catalog-model/ }))
    await user.type(screen.getByLabelText('API key'), 'first-secret')
    await user.click(screen.getByRole('button', { name: '+ Add key' }))
    await waitFor(() => expect(put).toHaveBeenCalledWith('/api/user/ai-credentials/openai%2Fcustom', { api_key: 'first-secret', model: 'catalog-model' })) // pragma: allowlist secret
    expect(await screen.findByRole('status')).toHaveTextContent('API key verified and saved.')
    expect(await screen.findByText('openai/custom')).toBeInTheDocument()
    expect(screen.getByLabelText('API key')).toHaveValue('')

    await user.click(picker)
    // A configured provider stays listed, marked as configured: hiding it made a
    // saved key look like it had been lost.
    expect((await screen.findAllByRole('option', /openai\/custom.*configured/)).length).toBeGreaterThan(0)
    await user.click(await screen.findByRole('option', { name: 'other' }))
    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(screen.getByLabelText('API key'), 'second-secret')
    await user.click(screen.getByRole('button', { name: '+ Add key' }))
    await waitFor(() => expect(put).toHaveBeenCalledWith('/api/user/ai-credentials/other', { api_key: 'second-secret', model: 'manual-model' })) // pragma: allowlist secret
    expect(screen.getAllByText('Configured')).toHaveLength(2)
    await user.click(screen.getByRole('button', { name: 'Remove openai/custom key' }))
    await waitFor(() => expect(remove).toHaveBeenCalledWith('/api/user/ai-credentials/openai%2Fcustom'))
    expect(within(screen.getByRole('list', { name: 'Configured AI providers' })).queryByText('openai/custom')).not.toBeInTheDocument()
    expect(screen.getByText('other')).toBeInTheDocument()
    expect(screen.getAllByText('Configured')).toHaveLength(1)
  })

  it('preserves leading and trailing whitespace in an opaque key', async () => {
    const user = userEvent.setup()
    render(<AiCredentials />)
    await user.click(await screen.findByRole('combobox', { name: 'AI provider' }))
    await user.click(await screen.findByRole('option', { name: 'other' }))
    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(screen.getByLabelText('API key'), '  secret-key  ')
    await user.click(screen.getByRole('button', { name: '+ Add key' }))
    await waitFor(() => expect(put).toHaveBeenCalledWith('/api/user/ai-credentials/other', { api_key: '  secret-key  ', model: 'manual-model' })) // pragma: allowlist secret
  })

  it('does not submit unsupported free text', async () => {
    const user = userEvent.setup()
    render(<AiCredentials />)
    const picker = await screen.findByRole('combobox', { name: 'AI provider' })
    await user.type(picker, 'unsupported')
    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'model')
    await user.type(screen.getByLabelText('API key'), 'secret-key')
    expect(screen.getByRole('button', { name: '+ Add key' })).toBeDisabled()
    expect(put).not.toHaveBeenCalled()
  })

  it('clears the key after a failed save and permits a retry', async () => {
    const user = userEvent.setup()
    put.mockRejectedValueOnce(new Error('failed'))
    render(<AiCredentials />)
    await user.click(await screen.findByRole('combobox', { name: 'AI provider' }))
    await user.click(await screen.findByRole('option', { name: 'other' }))
    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    const input = screen.getByLabelText('API key')
    await user.type(input, 'secret-key')
    await user.click(screen.getByRole('button', { name: '+ Add key' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Could not save API key'))
    expect(input).toHaveValue('')
    expect(screen.queryByText('Configured')).not.toBeInTheDocument()
    await user.type(input, 'new-key')
    await user.click(screen.getByRole('button', { name: '+ Add key' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
  })

  it('clears the key and locks controls while saving', async () => {
    const user = userEvent.setup()
    let resolve!: (value: unknown) => void
    put.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    render(<AiCredentials />)
    await user.click(await screen.findByRole('combobox', { name: 'AI provider' }))
    await user.click(await screen.findByRole('option', { name: 'other' }))
    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(screen.getByLabelText('API key'), 'transient-secret')
    await user.click(screen.getByRole('button', { name: '+ Add key' }))
    expect(screen.getByLabelText('API key')).toHaveValue('')
    expect(screen.getByRole('combobox', { name: 'AI provider' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '+ Add key' })).toBeDisabled()
    resolve({ outcome: 'accepted', ok: true })
    await waitFor(() => expect(screen.getByText('Configured')).toBeInTheDocument())
  })

  it('replaces a configured key without removing it first', async () => {
    const user = userEvent.setup()
    get.mockResolvedValueOnce({ providers: [{ provider: 'other', configured: true }] })
    render(<AiCredentials />)
    await user.click(await screen.findByRole('button', { name: 'Replace other key' }))
    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(screen.getByLabelText('API key'), 'replacement')
    await user.click(screen.getByRole('button', { name: 'Replace key' }))
    await waitFor(() => expect(put).toHaveBeenCalledWith('/api/user/ai-credentials/other', { api_key: 'replacement', model: 'manual-model' })) // pragma: allowlist secret
    expect(remove).not.toHaveBeenCalled()
    expect(screen.getByText('Configured')).toBeInTheDocument()
  })

  it('requires a model before verifying a key', async () => {
    const user = userEvent.setup()
    render(<AiCredentials />)
    await user.click(await screen.findByRole('combobox', { name: 'AI provider' }))
    await user.click(await screen.findByRole('option', { name: 'other' }))
    await user.type(screen.getByLabelText('API key'), 'secret-key')
    expect(screen.getByRole('button', { name: '+ Add key' })).toBeDisabled()
    expect(put).not.toHaveBeenCalled()
  })

  it.each(['rejected', 'inconclusive'] as const)('keeps the configured key after %s verification', async outcome => {
    const user = userEvent.setup()
    get.mockResolvedValueOnce({ providers: [{ provider: 'other', configured: true }] })
    put.mockResolvedValueOnce({ outcome, ok: false })
    render(<AiCredentials />)
    await user.click(await screen.findByRole('button', { name: 'Replace other key' }))
    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(screen.getByLabelText('API key'), 'replacement')
    await user.click(screen.getByRole('button', { name: 'Replace key' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(outcome === 'rejected' ? 'Check the key and selected model' : 'Try again when the provider is available')
    expect(screen.getByLabelText('API key')).toHaveValue('')
    expect(screen.getByText('Configured')).toBeInTheDocument()
    expect(screen.getByText('Replace other key')).toBeInTheDocument()
    expect(remove).not.toHaveBeenCalled()
  })

  it('does not show controls when loading fails', async () => {
    get.mockRejectedValueOnce(new Error('failed'))
    render(<AiCredentials />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load AI credentials')
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })
})

describe('AiCredentials with a configured but unusable key', () => {
  it('warns that a saved key returned no usable models', async () => {
    const user = userEvent.setup()
    catalogMocks.providerStatus = {
      'openai/custom': { has_api_key: true, model_count: 0, modelListingSupported: true },
    }
    get.mockResolvedValue({ providers: [{ provider: 'openai/custom', configured: true }] })

    render(<AiCredentials />)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Your openai/custom key is saved, but it returned no usable models.',
    )
    // The key must still be reachable, not silently hidden.
    await user.click(await screen.findByRole('combobox', { name: 'AI provider' }))
    expect((await screen.findAllByRole('option', /openai\/custom.*configured/)).length).toBeGreaterThan(0)
  })

  it('stays quiet for a manual-only key that cannot list models', async () => {
    catalogMocks.providerStatus = {
      'openai/custom': { has_api_key: true, model_count: 0, modelListingSupported: false },
    }
    get.mockResolvedValue({ providers: [{ provider: 'openai/custom', configured: true }] })

    render(<AiCredentials />)

    // Manual entry still works for this key, so it must not be called broken.
    expect(await screen.findByText('openai/custom')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('stays quiet when the configured key did return models', async () => {
    catalogMocks.providerStatus = {
      'openai/custom': { has_api_key: true, model_count: 2, modelListingSupported: true },
    }
    get.mockResolvedValue({ providers: [{ provider: 'openai/custom', configured: true }] })

    render(<AiCredentials />)

    expect(await screen.findByText('openai/custom')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
