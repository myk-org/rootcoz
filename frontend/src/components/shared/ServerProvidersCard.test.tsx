import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { api, ApiError } from '@/lib/api'
import { ServerProvidersCard } from './ServerProvidersCard'

Element.prototype.scrollIntoView = vi.fn()

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  extractApiDetail: () => null,
  ApiError: class ApiError extends Error {
    status: number
    statusText: string
    body: unknown
    constructor(status: number, statusText: string, body: unknown) {
      super(`API error ${status}: ${statusText}`)
      this.name = 'ApiError'
      this.status = status
      this.statusText = statusText
      this.body = body
    }
  },
}))
vi.mock('@/lib/useProviderOptions', () => ({
  resetProviderCatalogCache: vi.fn(),
  useProviderCatalog: () => ({
    providers: {
      gemini: [
        { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro' },
        { id: 'gemini-2.5-flash', name: 'Flash' },
      ],
      openai: [{ id: 'gpt-4o', name: 'GPT-4o' }],
      // Mixed-case catalog ID, exposed folded with its exact-case original.
      enmaas: [{ id: 'enmaas-f1', name: 'F1' }],
    },
    originalIds: { enmaas: 'EnMaaS' },
    providerStatus: {},
  }),
}))

const get = vi.mocked(api.get)
const post = vi.mocked(api.post)
const put = vi.mocked(api.put)
const del = vi.mocked(api.delete)

type Model = { id: string; name?: string; included: boolean; free: boolean }
type Provider = { provider: string; has_key: boolean; verified: boolean; supports_key: boolean; key_scoped?: boolean; models: Model[] }

function providersState(): Provider[] {
  return [
    {
      provider: 'gemini',
      has_key: true,
      verified: true,
      supports_key: true,
      models: [
        { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', included: true, free: false },
        { id: 'gemini-2.5-flash', name: 'Flash', included: false, free: false },
        { id: 'gemini-2.0', name: '', included: false, free: false },
      ],
    },
  ]
}

/** Un-keyed catalog entry — the server-providers list returns one per catalog provider. */
function catalogEntry(provider: string, supports_key: boolean): Provider {
  return { provider, has_key: false, verified: false, supports_key, models: [] }
}

/** Every catalog provider, all accepting session keys — the default addable state. */
function keyableState(): Provider[] {
  return [catalogEntry('gemini', true), catalogEntry('openai', true), catalogEntry('enmaas', true)]
}

/** GET response shape: {providers: [...]} — the real backend envelope. */
function listResult(providers: Provider[]) {
  return { providers } as never
}

beforeEach(() => {
  vi.clearAllMocks()
  get.mockResolvedValue(listResult(providersState()))
  // Real add/rotate response: HTTP 200 with ok true/false, not a throw.
  post.mockResolvedValue({ ok: true, outcome: 'accepted' } as never)
  put.mockResolvedValue({} as never)
  del.mockResolvedValue({ ok: true } as never)
})

async function openModels(user: ReturnType<typeof userEvent.setup>) {
  render(<ServerProvidersCard />)
  await screen.findByRole('list', { name: 'Configured server providers' })
  await user.click(screen.getByRole('button', { name: 'gemini models' }))
  await screen.findByRole('checkbox', { name: 'Select all' })
}

describe('ServerProvidersCard add flow gate', () => {
  it('keeps Add disabled until the provider test passes, then finishes the add', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult(keyableState()))
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('combobox', { name: 'Provider' }))
    await user.click(await screen.findByRole('option', { name: 'openai' }))
    await user.click(screen.getByRole('combobox', { name: 'Verification model' }))
    await user.click(await screen.findByRole('option', { name: /gpt-4o/ }))
    await user.type(screen.getByLabelText('API key'), 'sk-test')

    const addButton = screen.getByRole('button', { name: '+ Add provider' })
    expect(addButton).toBeDisabled()
    expect(post).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Test provider' }))
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/api/admin/server-providers', {
        provider: 'openai',
        api_key: 'sk-test', // pragma: allowlist secret
        verification_model: 'gpt-4o',
      }),
    )
    expect(await screen.findByRole('status')).toHaveTextContent('Provider verified')
    expect(addButton).toBeEnabled()

    await user.click(addButton)
    expect(await screen.findByRole('status')).toHaveTextContent('Server provider added.')
    // Form reset: the provider picker starts empty again
    expect(screen.getByRole('combobox', { name: 'Provider' })).toHaveValue('')
  })

  it('keeps Add disabled and clears the key when the test fails', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult(keyableState()))
    post.mockRejectedValueOnce(new Error('verification failed') as never)
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('combobox', { name: 'Provider' }))
    await user.click(await screen.findByRole('option', { name: 'openai' }))
    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(screen.getByLabelText('API key'), 'sk-bad')

    await user.click(screen.getByRole('button', { name: 'Test provider' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Provider verification failed')
    expect(screen.getByRole('button', { name: '+ Add provider' })).toBeDisabled()
    expect(screen.getByLabelText('API key')).toHaveValue('')
  })

  it('reports a rejected key from an ok:false response without verifying', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult(keyableState()))
    post.mockResolvedValueOnce({
      ok: false,
      outcome: 'rejected',
      message: 'Check the key and selected model; existing credential unchanged.',
    } as never)
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('combobox', { name: 'Provider' }))
    await user.click(await screen.findByRole('option', { name: 'openai' }))
    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(screen.getByLabelText('API key'), 'sk-bad')

    await user.click(screen.getByRole('button', { name: 'Test provider' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Check the key and selected model')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '+ Add provider' })).toBeDisabled()
  })

  it('falls back to the outcome when the ok:false response has no message', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult(keyableState()))
    post.mockResolvedValueOnce({ ok: false, outcome: 'inconclusive' } as never)
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('combobox', { name: 'Provider' }))
    await user.click(await screen.findByRole('option', { name: 'openai' }))
    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(screen.getByLabelText('API key'), 'sk-flaky')

    await user.click(screen.getByRole('button', { name: 'Test provider' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Key inconclusive')
    expect(screen.getByRole('button', { name: '+ Add provider' })).toBeDisabled()
  })

  it('submits a mixed-case catalog provider ID verbatim', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult(keyableState()))
    render(<ServerProvidersCard />)

    // The picker offers the catalog's exact-case ID, not the folded key
    await user.click(await screen.findByRole('combobox', { name: 'Provider' }))
    await user.click(await screen.findByRole('option', { name: 'EnMaaS' }))
    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'enmaas-f1')
    await user.type(screen.getByLabelText('API key'), 'sk-test')

    await user.click(screen.getByRole('button', { name: 'Test provider' }))
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/api/admin/server-providers', {
        provider: 'EnMaaS',
        api_key: 'sk-test', // pragma: allowlist secret
        verification_model: 'enmaas-f1',
      }),
    )
    expect(await screen.findByRole('status')).toHaveTextContent('Provider verified')
  })

  it('invalidates a passed test when the key changes', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult(keyableState()))
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('combobox', { name: 'Provider' }))
    await user.click(await screen.findByRole('option', { name: 'openai' }))
    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(screen.getByLabelText('API key'), 'sk-test')
    await user.click(screen.getByRole('button', { name: 'Test provider' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '+ Add provider' })).toBeEnabled())

    await user.type(screen.getByLabelText('API key'), '2')
    expect(screen.getByRole('button', { name: '+ Add provider' })).toBeDisabled()
  })
})

describe('ServerProvidersCard provider list', () => {
  it('lists keyless providers with a deployment-credentials hint and no key controls; they stay addable', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([
      catalogEntry('gemini', true),
      { provider: 'openai', has_key: true, verified: true, supports_key: true, models: [] },
    ]))
    render(<ServerProvidersCard />)

    // gemini has no stored key: still listed (deployment credentials), but
    // without rotate/remove buttons or a verified state
    expect(await screen.findByText('gemini')).toBeInTheDocument()
    expect(screen.getByText('Deployment credentials')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Rotate gemini key' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove gemini' })).not.toBeInTheDocument()
    expect(screen.queryByText('Unverified')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Rotate openai key' })).toBeInTheDocument()

    // A stored key can still be added for the keyless provider
    await user.click(screen.getByRole('combobox', { name: 'Provider' }))
    expect(await screen.findByRole('option', { name: 'gemini' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'openai' })).not.toBeInTheDocument()
  })

  it('badges a key-scoped provider and hints the static-catalog fallback otherwise (#350)', async () => {
    get.mockResolvedValue(listResult([
      { ...providersState()[0], key_scoped: true },
      { provider: 'openai', has_key: true, verified: true, supports_key: true, key_scoped: false, models: [] },
    ]))
    render(<ServerProvidersCard />)

    await screen.findByText('gemini')
    // One badge for the key-scoped provider, one hint for the keyed
    // provider without a captured listing — nothing else renders either.
    expect(screen.getAllByText('Key-scoped')).toHaveLength(1)
    expect(screen.getAllByText('static catalog')).toHaveLength(1)
  })

  it('refreshes key-scoped models with the stored key and reports the outcome (#350)', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([providersState()[0]]))
    post.mockResolvedValue({ ok: true, status: 'supported', model_count: 2 } as never)
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('button', { name: 'Refresh gemini models' }))
    expect(post).toHaveBeenCalledWith('/api/admin/server-providers/gemini/refresh-models')
    // The list is refetched and the notice reports the refreshed count.
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Refreshed key-scoped models for gemini: 2 model(s).',
    )
    expect(get).toHaveBeenCalledTimes(2)
  })

  it('reports the catalog fallback when the refreshed listing is unavailable (#350)', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([providersState()[0]]))
    post.mockResolvedValue({ ok: true, status: 'unavailable', model_count: 0 } as never)
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('button', { name: 'Refresh gemini models' }))
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Key-scoped listing unavailable for gemini; selection uses the static catalog.',
    )
  })

  it('shows an alert when the refresh fails (#350)', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([providersState()[0]]))
    post.mockRejectedValueOnce(new Error('boom'))
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('button', { name: 'Refresh gemini models' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not refresh models')
  })

  it('never offers catalog providers that cannot take session keys', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([
      catalogEntry('gemini', false), // ambient/deployment credentials
      catalogEntry('openai', true),
    ]))
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('combobox', { name: 'Provider' }))
    expect(await screen.findByRole('option', { name: 'openai' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'gemini' })).not.toBeInTheDocument()
  })

  it('gives keyless providers model selection and free management without key buttons', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([
      {
        provider: 'vertex-claude',
        has_key: false,
        verified: false,
        supports_key: false,
        models: [
          { id: 'claude-x', name: 'Claude X', included: true, free: true },
          { id: 'claude-y', name: 'Claude Y', included: false, free: false },
        ],
      },
    ]))
    render(<ServerProvidersCard />)

    expect(await screen.findByText('vertex-claude')).toBeInTheDocument()
    expect(screen.getByText('Deployment credentials')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Rotate vertex-claude key' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove vertex-claude' })).not.toBeInTheDocument()

    // Model scoping works
    await user.click(screen.getByRole('button', { name: 'vertex-claude models' }))
    await screen.findByRole('checkbox', { name: 'Select all' })
    expect(screen.getByRole('checkbox', { name: 'claude-x' })).toBeChecked()
    await user.click(screen.getByRole('checkbox', { name: 'claude-y' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/server-providers/vertex-claude/models', {
        models: [
          { id: 'claude-x', name: 'Claude X', included: true, free: true },
          { id: 'claude-y', name: 'Claude Y', included: true, free: false },
        ],
      }),
    )

    // Free pairs stay visible and removable
    const pill = screen.getByRole('button', { name: 'Toggle claude-x free' })
    expect(pill).toHaveAttribute('aria-pressed', 'true')
    await user.click(pill)
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/server-providers/vertex-claude/models', {
        models: [
          { id: 'claude-x', name: 'Claude X', included: true, free: false },
          { id: 'claude-y', name: 'Claude Y', included: true, free: false },
        ],
      }),
    )
    expect(screen.getByRole('button', { name: 'Toggle claude-x free' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('shows the empty state only when the server reports no providers at all', async () => {
    get.mockResolvedValue(listResult([]))
    render(<ServerProvidersCard />)

    expect(await screen.findByText('No server providers configured.')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Configured server providers' })).not.toBeInTheDocument()
  })
})

describe('ServerProvidersCard model multi-select', () => {
  it('shows per-model checkmarks, Select all with an indeterminate partial state, and saves', async () => {
    const user = userEvent.setup()
    await openModels(user)

    expect(screen.getByRole('checkbox', { name: 'gemini-2.5-pro' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'gemini-2.5-flash' })).not.toBeChecked()

    // Partial selection → Select all is indeterminate
    const selectAll = screen.getByRole('checkbox', { name: 'Select all' }) as HTMLInputElement
    expect(selectAll).not.toBeChecked()
    expect(selectAll.indeterminate).toBe(true)

    await user.click(selectAll)
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/server-providers/gemini/models', {
        models: [
          { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', included: true, free: false },
          { id: 'gemini-2.5-flash', name: 'Flash', included: true, free: false },
          { id: 'gemini-2.0', name: '', included: true, free: false },
        ],
      }),
    )
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    expect(screen.getByRole('checkbox', { name: 'Select all' })).toBeChecked()

    // Unchecking one makes Select all indeterminate again
    await user.click(screen.getByRole('checkbox', { name: 'gemini-2.0' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    expect((screen.getByRole('checkbox', { name: 'Select all' }) as HTMLInputElement).indeterminate).toBe(true)
  })

  it('keeps a queued successful edit visible when an earlier save fails', async () => {
    const user = userEvent.setup()
    let rejectFirst!: (reason?: unknown) => void
    put.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject }))
    put.mockResolvedValueOnce({} as never)
    await openModels(user)

    // Edit A is in flight (will fail); edit B is queued behind it and succeeds
    await user.click(screen.getByRole('checkbox', { name: 'gemini-2.5-flash' }))
    await user.click(screen.getByRole('checkbox', { name: 'gemini-2.0' }))

    rejectFirst(new Error('save failed'))
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    // B's saved result stays visible — A's failure must not roll it back
    expect(screen.getByRole('checkbox', { name: 'gemini-2.5-flash' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'gemini-2.0' })).toBeChecked()
  })

  it('restores the refreshed baseline when a save fails after a refetch', async () => {
    const user = userEvent.setup()
    const before = providersState()
    const after = providersState()
    // Another admin included flash while we held the pre-refetch snapshot
    after[0].models[1] = { id: 'gemini-2.5-flash', name: 'Flash', included: true, free: false }
    let refetched = false
    get.mockImplementation(async () => listResult(refetched ? after : before))
    render(<ServerProvidersCard />)

    // A rotation refetches the provider list → the draft is replaced from
    // the refreshed models
    await user.click(await screen.findByRole('button', { name: 'Rotate gemini key' }))
    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(screen.getByLabelText('API key'), 'new-secret')
    refetched = true
    await user.click(screen.getByRole('button', { name: 'Test provider' }))
    expect(await screen.findByRole('status')).toHaveTextContent('API key verified and rotated.')

    put.mockRejectedValueOnce(new Error('save failed') as never)
    await user.click(screen.getByRole('button', { name: 'gemini models' }))
    await screen.findByRole('checkbox', { name: 'Select all' })
    await user.click(screen.getByRole('checkbox', { name: 'gemini-2.0' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save models')
    // Rolled back to the refreshed snapshot, not the pre-refetch selection
    expect(screen.getByRole('checkbox', { name: 'gemini-2.5-flash' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'gemini-2.0' })).not.toBeChecked()
  })

  it('filters the model list', async () => {
    const user = userEvent.setup()
    await openModels(user)

    await user.type(screen.getByRole('textbox', { name: 'Filter models' }), 'flash')
    expect(screen.getByRole('checkbox', { name: 'gemini-2.5-flash' })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'gemini-2.5-pro' })).not.toBeInTheDocument()
  })

  it('scopes Select all to the filtered list, leaving hidden models untouched', async () => {
    const user = userEvent.setup()
    // Hidden pro is included AND free — both flags must survive Select all
    const state = providersState()
    state[0].models[0] = { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', included: true, free: true }
    get.mockResolvedValue(listResult(state))
    await openModels(user)

    await user.type(screen.getByRole('textbox', { name: 'Filter models' }), 'flash')

    // Only flash is visible and none of the visible models is included →
    // unchecked, not indeterminate, even though pro is included overall
    const selectAll = screen.getByRole('checkbox', { name: 'Select all' }) as HTMLInputElement
    expect(selectAll).not.toBeChecked()
    expect(selectAll.indeterminate).toBe(false)

    await user.click(selectAll)
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/server-providers/gemini/models', {
        models: [
          { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', included: true, free: true },
          { id: 'gemini-2.5-flash', name: 'Flash', included: true, free: false },
          { id: 'gemini-2.0', name: '', included: false, free: false },
        ],
      }),
    )
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    // All visible models included → checked
    expect(screen.getByRole('checkbox', { name: 'Select all' })).toBeChecked()

    // Unchecking deselects only the visible model; hidden flags untouched
    await user.click(screen.getByRole('checkbox', { name: 'Select all' }))
    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith('/api/admin/server-providers/gemini/models', {
        models: [
          { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', included: true, free: true },
          { id: 'gemini-2.5-flash', name: 'Flash', included: false, free: false },
          { id: 'gemini-2.0', name: '', included: false, free: false },
        ],
      }),
    )
    expect((screen.getByRole('checkbox', { name: 'Select all' }) as HTMLInputElement).indeterminate).toBe(false)
  })

  it('marks Select all indeterminate over the filtered subset', async () => {
    const user = userEvent.setup()
    await openModels(user)

    // pro (included) and flash (not) are visible; gemini-2.0 is hidden
    await user.type(screen.getByRole('textbox', { name: 'Filter models' }), '2.5')
    const selectAll = screen.getByRole('checkbox', { name: 'Select all' }) as HTMLInputElement
    expect(selectAll).not.toBeChecked()
    expect(selectAll.indeterminate).toBe(true)

    // Selecting all visible models flips only those two
    await user.click(selectAll)
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/server-providers/gemini/models', {
        models: [
          { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', included: true, free: false },
          { id: 'gemini-2.5-flash', name: 'Flash', included: true, free: false },
          { id: 'gemini-2.0', name: '', included: false, free: false },
        ],
      }),
    )
    expect(screen.getByRole('checkbox', { name: 'Select all' })).toBeChecked()
  })
})

describe('ServerProvidersCard free pill', () => {
  it('marks a model free and auto-includes it', async () => {
    const user = userEvent.setup()
    await openModels(user)

    const pill = screen.getByRole('button', { name: 'Toggle gemini-2.5-flash free' })
    expect(pill).toHaveAttribute('aria-pressed', 'false')

    await user.click(pill)
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/server-providers/gemini/models', {
        models: [
          { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', included: true, free: false },
          { id: 'gemini-2.5-flash', name: 'Flash', included: true, free: true },
          { id: 'gemini-2.0', name: '', included: false, free: false },
        ],
      }),
    )
    expect(screen.getByRole('button', { name: 'Toggle gemini-2.5-flash free' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('checkbox', { name: 'gemini-2.5-flash' })).toBeChecked()
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
  })

  it('clears the free flag when the model is un-included', async () => {
    const user = userEvent.setup()
    const state = providersState()
    state[0].models[1] = { id: 'gemini-2.5-flash', name: 'Flash', included: true, free: true }
    get.mockResolvedValue(listResult(state))
    await openModels(user)

    expect(screen.getByRole('button', { name: 'Toggle gemini-2.5-flash free' })).toHaveAttribute('aria-pressed', 'true')

    await user.click(screen.getByRole('checkbox', { name: 'gemini-2.5-flash' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/server-providers/gemini/models', {
        models: [
          { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', included: true, free: false },
          { id: 'gemini-2.5-flash', name: 'Flash', included: false, free: false },
          { id: 'gemini-2.0', name: '', included: false, free: false },
        ],
      }),
    )
    expect(screen.getByRole('button', { name: 'Toggle gemini-2.5-flash free' })).toHaveAttribute('aria-pressed', 'false')
  })
})

describe('ServerProvidersCard remove and rotate', () => {
  it('confirms removal from loaded state before sending a single DELETE', async () => {
    const user = userEvent.setup()
    let removed = false
    const state = providersState()
    state[0].models[1] = { id: 'gemini-2.5-flash', name: 'Flash', included: true, free: true }
    get.mockImplementation(async () => listResult(removed ? [] : state))
    del.mockImplementation(async () => {
      removed = true
      return { ok: true } as never
    })
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('button', { name: 'Remove gemini' }))

    // The dialog opens before any DELETE, listing the affected selected and
    // free models from the loaded provider entry
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/gemini-2\.5-pro/)).toBeInTheDocument()
    expect(within(dialog).getByText(/gemini-2\.5-flash/)).toBeInTheDocument()
    expect(del).not.toHaveBeenCalled()

    await user.click(within(dialog).getByRole('button', { name: 'Remove provider' }))
    await waitFor(() =>
      expect(del).toHaveBeenLastCalledWith('/api/admin/server-providers/gemini'),
    )
    expect(del).toHaveBeenCalledTimes(1)
    await waitFor(() =>
      expect(screen.queryByRole('list', { name: 'Configured server providers' })).not.toBeInTheDocument(),
    )
    expect(await screen.findByRole('status')).toHaveTextContent('gemini removed.')
  })

  it('confirms removal even when no models are affected', async () => {
    const user = userEvent.setup()
    let removed = false
    get.mockImplementation(async () => listResult(removed ? [] : providersState()))
    del.mockImplementation(async () => {
      removed = true
      return { ok: true } as never
    })
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('button', { name: 'Remove gemini' }))

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/none/)).toBeInTheDocument()
    expect(del).not.toHaveBeenCalled()

    await user.click(within(dialog).getByRole('button', { name: 'Remove provider' }))
    await waitFor(() =>
      expect(screen.queryByRole('list', { name: 'Configured server providers' })).not.toBeInTheDocument(),
    )
    expect(del).toHaveBeenCalledTimes(1)
  })

  it('treats a 404 on confirm as already removed', async () => {
    const user = userEvent.setup()
    del.mockRejectedValueOnce(new ApiError(404, 'Not Found', { detail: 'not configured' }) as never)
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('button', { name: 'Remove gemini' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Remove provider' }))

    expect(await screen.findByRole('status')).toHaveTextContent('gemini removed.')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows an error when removal fails for another reason', async () => {
    const user = userEvent.setup()
    del.mockRejectedValueOnce(new ApiError(500, 'Server Error', null) as never)
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('button', { name: 'Remove gemini' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Remove provider' }))

    expect(await screen.findByText('Could not remove provider')).toBeInTheDocument()
    // The dialog stays open so the admin can retry
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('rotates a key via the rotate-key endpoint with the provider in the body', async () => {
    const user = userEvent.setup()
    render(<ServerProvidersCard />)
    await user.click(await screen.findByRole('button', { name: 'Rotate gemini key' }))

    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(screen.getByLabelText('API key'), 'new-secret')
    await user.click(screen.getByRole('button', { name: 'Test provider' }))

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/api/admin/server-providers/gemini/rotate-key', {
        provider: 'gemini',
        api_key: 'new-secret', // pragma: allowlist secret
        verification_model: 'manual-model',
      }),
    )
    expect(await screen.findByRole('status')).toHaveTextContent('API key verified and rotated.')
  })

  it('reports an ok:false rotation without clearing the editing form', async () => {
    const user = userEvent.setup()
    post.mockResolvedValueOnce({ ok: false, outcome: 'rejected' } as never)
    render(<ServerProvidersCard />)
    await user.click(await screen.findByRole('button', { name: 'Rotate gemini key' }))

    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(screen.getByLabelText('API key'), 'bad-secret')
    await user.click(screen.getByRole('button', { name: 'Test provider' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Key rejected')
    // Still editing: the rotate form is not closed on a rejected key
    expect(screen.getByText('Rotate gemini key')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})
