import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { api, ApiError } from '@/lib/api'
import { resetProviderCatalogCache } from '@/lib/useProviderOptions'
import { ServerProvidersCard } from './ServerProvidersCard'

Element.prototype.scrollIntoView = vi.fn()

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  extractApiDetail: (err: unknown) => {
    const detail = (err as { body?: { detail?: unknown } } | null)?.body?.detail
    return typeof detail === 'string' ? detail : null
  },
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
type Provider = { provider: string; has_key: boolean; verified: boolean; supports_key: boolean; can_refresh_models?: boolean; has_deployment_credential?: boolean; key_scoped?: boolean; key_scoped_status?: string | null; key_scoped_source?: 'deployment' | 'stored' | null; models: Model[] }

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
function catalogEntry(provider: string, supports_key: boolean, can_refresh_models?: boolean): Provider {
  return { provider, has_key: false, verified: false, supports_key, can_refresh_models, models: [] }
}

/** Registry-only entry: no key, no deployment listing, no models — addable, never a row. */
function registryEntry(provider: string): Provider {
  return { ...catalogEntry(provider, true), key_scoped_source: null }
}

/** Keyless provider running on the deployment's credential — a row with Store API key. */
function deploymentEntry(provider: string, supports_key = true, can_refresh_models?: boolean, models: Model[] = []): Provider {
  return { ...catalogEntry(provider, supports_key, can_refresh_models), has_deployment_credential: true, key_scoped_source: 'deployment', models }
}

/** Every catalog provider, all accepting session keys — the default addable state. */
function keyableState(): Provider[] {
  return [catalogEntry('gemini', true), catalogEntry('openai', true), catalogEntry('enmaas', true)]
}

/** Keyed gemini row + keyless openai on deployment credentials — what the Store/Edit key dialogs act on. */
function keyDialogState(): Provider[] {
  return [providersState()[0], deploymentEntry('openai')]
}

async function openStoreDialog(user: ReturnType<typeof userEvent.setup>, provider = 'openai') {
  await user.click(await screen.findByRole('button', { name: `Store ${provider} API key` }))
  return screen.findByRole('dialog')
}

async function openEditDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: 'Edit gemini key' }))
  return screen.findByRole('dialog')
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
  it('lists keyless providers with a deployment-credentials hint and no key controls; not offered in Find a provider', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([
      deploymentEntry('gemini'),
      { provider: 'openai', has_key: true, verified: true, supports_key: true, models: [] },
      registryEntry('anthropic'),
    ]))
    render(<ServerProvidersCard />)

    // gemini has no stored key: still listed (deployment credentials), but
    // without rotate/remove buttons or a verified state
    expect(await screen.findByText('gemini')).toBeInTheDocument()
    expect(screen.getByText('Deployment credentials')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit gemini key' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove gemini' })).not.toBeInTheDocument()
    expect(screen.queryByText('Unverified')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit openai key' })).toBeInTheDocument()

    // Both rows already own their key action — only the unconfigured registry
    // provider is left to add.
    await user.click(screen.getByRole('combobox', { name: 'Provider' }))
    expect(await screen.findByRole('option', { name: 'anthropic' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'gemini' })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'openai' })).not.toBeInTheDocument()
  })

  it('hints the deployment-credential listing source without implying a stored key', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([
      { provider: 'EnMaaS', has_key: false, verified: false, supports_key: false, has_deployment_credential: true, key_scoped_source: 'deployment', models: [] },
    ]))
    render(<ServerProvidersCard />)

    const hint = await screen.findByText('Listing from deployment credential')
    // has_key is false for deployment credentials — no verified/stored-key state
    expect(screen.getByText('Deployment credentials')).toBeInTheDocument()
    expect(screen.queryByText('Verified')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit EnMaaS key' })).not.toBeInTheDocument()

    await user.hover(hint)
    expect(await screen.findByRole('tooltip')).toHaveTextContent(
      'Model list is verified live against the deployment-provided key — no key is stored here.',
    )
  })

  it('shows the deployment hint only for the deployment listing source', async () => {
    get.mockResolvedValue(listResult([
      { ...providersState()[0], key_scoped_source: 'stored' },
      { provider: 'openai', has_key: true, verified: true, supports_key: true, key_scoped_source: null, models: [] },
    ]))
    render(<ServerProvidersCard />)

    await screen.findByText('gemini')
    expect(screen.queryByText('Listing from deployment credential')).not.toBeInTheDocument()
  })

  it('keeps the unavailable-listing warning alongside a stored listing source', async () => {
    get.mockResolvedValue(listResult([
      {
        ...providersState()[0],
        key_scoped: true,
        key_scoped_status: 'unavailable',
        key_scoped_source: 'stored',
      },
    ]))
    render(<ServerProvidersCard />)

    await screen.findByText('gemini')
    expect(screen.getByText('Key-scoped')).toBeInTheDocument()
    expect(screen.getByText('listing failed — showing last capture')).toBeInTheDocument()
    expect(screen.queryByText('Listing from deployment credential')).not.toBeInTheDocument()
  })

  it('renders every model of a full deployment listing (#372: 23 for EnMaaS)', async () => {
    const user = userEvent.setup()
    const models = Array.from({ length: 23 }, (_, i) => ({
      id: `enmaas-model-${i + 1}`,
      name: `EnMaaS Model ${i + 1}`,
      included: true,
      free: false,
    }))
    get.mockResolvedValue(listResult([
      { provider: 'EnMaaS', has_key: false, verified: false, supports_key: false, has_deployment_credential: true, key_scoped_source: 'deployment', models },
    ]))
    render(<ServerProvidersCard />)

    expect(await screen.findByRole('button', { name: 'EnMaaS models' })).toHaveTextContent('Models (23 of 23)')
    await user.click(screen.getByRole('button', { name: 'EnMaaS models' }))
    await screen.findByRole('checkbox', { name: 'Select all' })

    // Select all + one checkbox per model — no slicing anywhere
    expect(screen.getAllByRole('checkbox')).toHaveLength(24)
    expect(screen.getByRole('checkbox', { name: 'enmaas-model-1' })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'enmaas-model-23' })).toBeInTheDocument()
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

  it('hints a failed listing differently from a missing listing endpoint (#350 r2)', async () => {
    get.mockResolvedValue(listResult([
      // Discovery failed for this key — the hint must suggest a refresh,
      // not claim the provider has no listing endpoint.
      { ...providersState()[0], key_scoped: false, key_scoped_status: 'unavailable' },
      { provider: 'openai', has_key: true, verified: true, supports_key: true, key_scoped: false, key_scoped_status: 'unsupported', models: [] },
    ]))
    render(<ServerProvidersCard />)

    await screen.findByText('gemini')
    expect(screen.getAllByText('listing failed')).toHaveLength(1)
    expect(screen.getAllByText('static catalog')).toHaveLength(1)
    // The failed-listing hint points at Refresh models as the remedy.
    expect(screen.getByTitle('Model listing failed — try Refresh models; until then selection uses the static catalog')).toBeInTheDocument()
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

  it('hints a stale capture when a key-scoped listing refresh failed (#350 r3)', async () => {
    get.mockResolvedValue(listResult([
      // Rows still exist (key_scoped) but the last refresh failed — the
      // failure must surface next to the Key-scoped badge, not hide it.
      { ...providersState()[0], key_scoped: true, key_scoped_status: 'unavailable' },
    ]))
    render(<ServerProvidersCard />)

    await screen.findByText('gemini')
    expect(screen.getByText('Key-scoped')).toBeInTheDocument()
    expect(screen.getByText('listing failed — showing last capture')).toBeInTheDocument()
    expect(screen.getByTitle('Model listing failed — showing the last captured list; try Refresh models')).toBeInTheDocument()
  })

  it('reports the kept listing when a refresh fails over an existing snapshot (#350 r3)', async () => {
    const user = userEvent.setup()
    get.mockResolvedValueOnce(listResult([providersState()[0]]))
      .mockResolvedValueOnce(listResult([
        { ...providersState()[0], key_scoped: true, key_scoped_status: 'unavailable' },
      ]))
    post.mockResolvedValue({ ok: true, status: 'unavailable', model_count: 0 } as never)
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('button', { name: 'Refresh gemini models' }))
    // The refetched list still carries a snapshot — the notice must say
    // the previous listing is in use, not imply a static-catalog fallback.
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Key-scoped listing unavailable for gemini; the previous captured listing is still in use.',
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
      deploymentEntry('vertex-claude', false, undefined, [
        { id: 'claude-x', name: 'Claude X', included: true, free: true },
        { id: 'claude-y', name: 'Claude Y', included: false, free: false },
      ]),
    ]))
    render(<ServerProvidersCard />)

    expect(await screen.findByText('vertex-claude')).toBeInTheDocument()
    expect(screen.getByText('Deployment credentials')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit vertex-claude key' })).not.toBeInTheDocument()
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

  it('offers a registry-only key-capable provider that has no catalog models', async () => {
    const user = userEvent.setup()
    // anthropic is registered in the sidecar but absent from the live catalog
    get.mockResolvedValue(listResult([registryEntry('anthropic')]))
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('combobox', { name: 'Provider' }))
    expect(await screen.findByRole('option', { name: 'anthropic' })).toBeInTheDocument()
    // It has no catalog models, so the verification picker has no options —
    // free-text entry still works.
    await user.click(screen.getByRole('option', { name: 'anthropic' }))
    const modelField = screen.getByRole('combobox', { name: 'Verification model' })
    expect(modelField).toBeEnabled()
    await user.type(modelField, 'claude-x')
    expect(modelField).toHaveValue('claude-x')
  })

  it('does not offer a registry-only provider that cannot take session keys', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([catalogEntry('anthropic', false), registryEntry('openai')]))
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('combobox', { name: 'Provider' }))
    expect(await screen.findByRole('option', { name: 'openai' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'anthropic' })).not.toBeInTheDocument()
  })

  it('does not offer a provider that already has a stored key', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([
      { provider: 'anthropic', has_key: true, verified: true, supports_key: true, models: [] },
      catalogEntry('openai', true),
    ]))
    render(<ServerProvidersCard />)

    // anthropic has Edit instead of an add option
    expect(await screen.findByRole('button', { name: 'Edit anthropic key' })).toBeInTheDocument()
    await user.click(screen.getByRole('combobox', { name: 'Provider' }))
    expect(await screen.findByRole('option', { name: 'openai' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'anthropic' })).not.toBeInTheDocument()
  })

  it('lists a provider present in both the catalog and the listing exactly once', async () => {
    const user = userEvent.setup()
    // openai is in the catalog and in the listing — the union must not duplicate it
    get.mockResolvedValue(listResult([registryEntry('openai'), registryEntry('anthropic')]))
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('combobox', { name: 'Provider' }))
    await screen.findByRole('option', { name: 'anthropic' })
    expect(screen.getAllByRole('option', { name: 'openai' })).toHaveLength(1)
    expect(screen.getAllByRole('option', { name: 'anthropic' })).toHaveLength(1)
  })

  it('renders no row for a registry-only provider but still offers it in Find a provider', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([registryEntry('anthropic')]))
    render(<ServerProvidersCard />)

    // Nothing in play: no row, no misleading Deployment credentials badge or
    // Store API key button that would 404 — just the empty state.
    expect(await screen.findByText('No server providers configured.')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Configured server providers' })).not.toBeInTheDocument()
    expect(screen.queryByText('Deployment credentials')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Store .* API key/ })).not.toBeInTheDocument()

    await user.click(screen.getByRole('combobox', { name: 'Provider' }))
    expect(await screen.findByRole('option', { name: 'anthropic' })).toBeInTheDocument()
  })

  it('renders only the in-play rows of a registry-heavy listing', async () => {
    get.mockResolvedValue(listResult([
      ...['amazon-bedrock', 'ant-ling', 'anthropic', 'azure', 'baseten', 'cerebras', 'cloudflare-ai-gateway'].map(registryEntry),
      providersState()[0],
      deploymentEntry('openai', true, true, [{ id: 'gpt-4o', name: 'GPT-4o', included: true, free: false }]),
    ]))
    render(<ServerProvidersCard />)

    const list = await screen.findByRole('list', { name: 'Configured server providers' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(2)
    expect(within(list).getByText('gemini')).toBeInTheDocument()
    expect(within(list).getByText('openai')).toBeInTheDocument()
    for (const junk of ['amazon-bedrock', 'ant-ling', 'anthropic', 'azure', 'baseten', 'cerebras', 'cloudflare-ai-gateway']) {
      expect(within(list).queryByText(junk)).not.toBeInTheDocument()
    }
    // No wall of "Models (0 of 0)" rows
    expect(screen.queryByText('Models (0 of 0)')).not.toBeInTheDocument()
  })

  it('keeps a keyless catalog provider with models addable instead of making it a row (#380)', async () => {
    const user = userEvent.setup()
    // Catalog models are not a credential: with no stored key and no
    // deployment credential the provider must stay addable.
    get.mockResolvedValue(listResult([{
      ...catalogEntry('openai', true, true),
      models: [{ id: 'gpt-4o', name: 'GPT-4o', included: true, free: false }],
    }]))
    render(<ServerProvidersCard />)

    expect(await screen.findByText('No server providers configured.')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Configured server providers' })).not.toBeInTheDocument()
    expect(screen.queryByText('Deployment credentials')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Store .* API key/ })).not.toBeInTheDocument()

    await user.click(screen.getByRole('combobox', { name: 'Provider' }))
    expect(await screen.findByRole('option', { name: 'openai' })).toBeInTheDocument()
  })

  it('offers Store API key for a deployment credential without a captured listing (#380)', async () => {
    const user = userEvent.setup()
    // key_scoped_source describes the listing only — it is null here, yet the
    // credential is resolvable, so the row can still promote it.
    get.mockResolvedValue(listResult([
      { ...catalogEntry('openai', true), has_deployment_credential: true, key_scoped_source: null },
      registryEntry('anthropic'),
    ]))
    render(<ServerProvidersCard />)

    const list = await screen.findByRole('list', { name: 'Configured server providers' })
    expect(within(list).getByText('openai')).toBeInTheDocument()
    expect(screen.getByText('Deployment credentials')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Store openai API key' })).toBeInTheDocument()
    expect(screen.queryByText('Listing from deployment credential')).not.toBeInTheDocument()

    await user.click(screen.getByRole('combobox', { name: 'Provider' }))
    expect(await screen.findByRole('option', { name: 'anthropic' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'openai' })).not.toBeInTheDocument()
  })

  it('shows the empty state when no provider is in play', async () => {
    get.mockResolvedValue(listResult([]))
    render(<ServerProvidersCard />)

    expect(await screen.findByText('No server providers configured.')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Configured server providers' })).not.toBeInTheDocument()
  })
})

describe('ServerProvidersCard Find a provider de-duplication', () => {
  it('does not offer a deployment-credential row; its row actions stay', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([deploymentEntry('openai', true, true), registryEntry('anthropic')]))
    render(<ServerProvidersCard />)

    // The row owns the key action — the admin uses Store API key, not add
    expect(await screen.findByRole('button', { name: 'Store openai API key' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh openai models' })).toBeInTheDocument()

    await user.click(screen.getByRole('combobox', { name: 'Provider' }))
    expect(await screen.findByRole('option', { name: 'anthropic' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'openai' })).not.toBeInTheDocument()
  })

  it('still offers a catalog provider whose only models are unbacked by a credential (#380)', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([
      { ...catalogEntry('openai', true, true), models: [{ id: 'gpt-4o', name: 'GPT-4o', included: true, free: false }] },
      registryEntry('anthropic'),
    ]))
    render(<ServerProvidersCard />)

    await screen.findByText('No server providers configured.')
    await user.click(screen.getByRole('combobox', { name: 'Provider' }))
    expect(await screen.findByRole('option', { name: 'anthropic' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'openai' })).toBeInTheDocument()
  })

  it('offers only the providers that have no row', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([
      providersState()[0], // gemini, stored key
      deploymentEntry('openai', true, true), // openai on deployment credentials
      { ...catalogEntry('cerebras', true), models: [{ id: 'llama-x', included: false, free: false }] },
      registryEntry('google'),
      registryEntry('openrouter'),
      registryEntry('amazon-bedrock'),
      registryEntry('ant-ling'),
    ]))
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('combobox', { name: 'Provider' }))
    await screen.findByRole('option', { name: 'google' })
    for (const unconfigured of ['google', 'openrouter', 'amazon-bedrock', 'ant-ling']) {
      expect(screen.getByRole('option', { name: unconfigured })).toBeInTheDocument()
    }
    // Credentialed rows are not offered; a models-only provider still is.
    for (const row of ['gemini', 'openai']) {
      expect(screen.queryByRole('option', { name: row })).not.toBeInTheDocument()
    }
    expect(screen.getByRole('option', { name: 'cerebras' })).toBeInTheDocument()
    expect(screen.getAllByRole('option')).toHaveLength(5)
  })
})

describe('ServerProvidersCard deployment-credential controls', () => {
  it('refreshes models on a keyless provider when the backend allows it', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([deploymentEntry('gemini', true, true)]))
    post.mockResolvedValue({ ok: true, status: 'supported', model_count: 2 } as never)
    render(<ServerProvidersCard />)

    // No stored key — the row runs on deployment credentials
    expect(await screen.findByText('Deployment credentials')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Refresh gemini models' }))
    expect(post).toHaveBeenCalledWith('/api/admin/server-providers/gemini/refresh-models')
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Refreshed key-scoped models for gemini: 2 model(s).',
    )
  })

  it('hides Refresh models on a keyless provider the backend marks non-refreshable', async () => {
    get.mockResolvedValue(listResult([deploymentEntry('gemini', true, false)]))
    render(<ServerProvidersCard />)

    expect(await screen.findByText('Deployment credentials')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Refresh .* models/ })).not.toBeInTheDocument()
  })

  it('opens the store dialog with an empty model field and a closed picker (#378)', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult(keyDialogState()))
    render(<ServerProvidersCard />)

    const dialog = await openStoreDialog(user)
    const modelField = within(dialog).getByRole('combobox', { name: 'Verification model' })
    // No default model and nothing autofocused the combobox, so the
    // focus-triggered dropdown is not already open.
    expect(modelField).toHaveValue('')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('option')).not.toBeInTheDocument()

    const store = within(dialog).getByRole('button', { name: 'Store key' })
    expect(store).toBeDisabled()
    await user.type(modelField, 'gpt-4o')
    expect(store).toBeEnabled()

    await user.click(store)
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        '/api/admin/server-providers/openai/store-deployment-key',
        { verification_model: 'gpt-4o' },
      ),
    )
    // Nothing pasted: the body carries the model only
    expect(Object.keys(post.mock.calls[0][1] as object)).toEqual(['verification_model'])
    // Success closes the dialog, refreshes the list and reports the notice
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(await screen.findByRole('status')).toHaveTextContent('openai deployment credential stored.')
    expect(resetProviderCatalogCache).toHaveBeenCalled()
    expect(get).toHaveBeenCalledTimes(2)
  })

  it('keeps the store dialog open with the server message when nothing is stored (#378)', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult(keyDialogState()))
    post.mockResolvedValueOnce({
      ok: false,
      outcome: 'rejected',
      message: 'Check the selected model; nothing was stored.',
    } as never)
    render(<ServerProvidersCard />)

    const dialog = await openStoreDialog(user)
    await user.type(within(dialog).getByRole('combobox', { name: 'Verification model' }), 'wrong-model')
    await user.click(within(dialog).getByRole('button', { name: 'Store key' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('nothing was stored')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(get).toHaveBeenCalledTimes(1)
  })

  it('keeps the store dialog open with the server detail on a 404/409 (#378)', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult(keyDialogState()))
    post.mockRejectedValueOnce(new ApiError(409, 'Conflict', {
      detail: "Provider 'openai' already has a stored key; use rotate-key",
    }) as never)
    render(<ServerProvidersCard />)

    const dialog = await openStoreDialog(user)
    await user.type(within(dialog).getByRole('combobox', { name: 'Verification model' }), 'gpt-4o')
    await user.click(within(dialog).getByRole('button', { name: 'Store key' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('already has a stored key')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('stores the deployment credential of a provider outside the live catalog (#378)', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([
      { provider: 'vertex-claude', has_key: false, verified: false, supports_key: true, can_refresh_models: true, has_deployment_credential: true, key_scoped_source: 'deployment', models: [] },
    ]))
    render(<ServerProvidersCard />)

    // The dialog is bound to the row's provider — no provider picker needed
    const dialog = await openStoreDialog(user, 'vertex-claude')
    expect(within(dialog).queryByRole('combobox', { name: 'Provider' })).not.toBeInTheDocument()
    await user.type(within(dialog).getByRole('combobox', { name: 'Verification model' }), 'claude-x')
    await user.click(within(dialog).getByRole('button', { name: 'Store key' }))

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        '/api/admin/server-providers/vertex-claude/store-deployment-key',
        { verification_model: 'claude-x' },
      ),
    )
  })

  it('stores a key for a provider with no models listed, letting the server discover one (#378)', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([deploymentEntry('vertex-claude', true, true)]))
    render(<ServerProvidersCard />)

    const dialog = await openStoreDialog(user, 'vertex-claude')
    // Nothing to choose from — the field stays empty and submit is not blocked
    expect(within(dialog).getByRole('combobox', { name: 'Verification model' })).toHaveValue('')
    expect(within(dialog).getByText(/a model will be discovered from the credential/)).toBeInTheDocument()
    const store = within(dialog).getByRole('button', { name: 'Store key' })
    expect(store).toBeEnabled()

    await user.click(store)
    // The field is omitted entirely — never sent empty — so the server picks one
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        '/api/admin/server-providers/vertex-claude/store-deployment-key',
        {},
      ),
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(await screen.findByRole('status')).toHaveTextContent('vertex-claude deployment credential stored.')
    expect(get).toHaveBeenCalledTimes(2)
  })

  it('saves a rotated key for a provider with no models listed without a verification model (#378)', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([{ ...providersState()[0], provider: 'vertex-claude', models: [] }]))
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('button', { name: 'Edit vertex-claude key' }))
    const dialog = await screen.findByRole('dialog')
    const save = within(dialog).getByRole('button', { name: 'Save key' })
    // The key still gates submit; the missing model does not
    expect(save).toBeDisabled()
    await user.type(within(dialog).getByLabelText('New API key'), 'new-secret')
    expect(save).toBeEnabled()

    await user.click(save)
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        '/api/admin/server-providers/vertex-claude/rotate-key',
        { provider: 'vertex-claude', api_key: 'new-secret' }, // pragma: allowlist secret
      ),
    )
    expect(Object.keys(post.mock.calls[0][1] as object)).not.toContain('verification_model')
  })

  it('still requires a model when the provider lists models to choose from (#378)', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult(keyDialogState()))
    render(<ServerProvidersCard />)

    const dialog = await openStoreDialog(user)
    expect(within(dialog).queryByText(/a model will be discovered from the credential/)).not.toBeInTheDocument()
    expect(within(dialog).getByText(/enter its ID manually/)).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Store key' })).toBeDisabled()
  })

  it('keeps the dialog open with the server message when no model is discoverable (#378)', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([deploymentEntry('vertex-claude', true, true)]))
    post.mockRejectedValueOnce(new ApiError(400, 'Bad Request', {
      detail: "Could not discover a model with this credential for 'vertex-claude'; enter a model id to verify against.",
    }) as never)
    render(<ServerProvidersCard />)

    const dialog = await openStoreDialog(user, 'vertex-claude')
    await user.click(within(dialog).getByRole('button', { name: 'Store key' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('enter a model id to verify against')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('hints Store API key with what it promotes (#378)', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([deploymentEntry('openai', true, true)]))
    render(<ServerProvidersCard />)

    await user.hover(await screen.findByRole('button', { name: 'Store openai API key' }))
    expect(await screen.findByRole('tooltip')).toHaveTextContent('Promotes the deployment credential')
  })

  it('leaves Refresh models without a tooltip (#378)', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue(listResult([deploymentEntry('openai', true, true)]))
    render(<ServerProvidersCard />)

    await user.hover(await screen.findByRole('button', { name: 'Refresh openai models' }))
    // Longer than the row's tooltip delay — nothing should appear
    await new Promise((resolve) => setTimeout(resolve, 300))
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('keeps each keyed-row control exactly once and offers no Store API key', async () => {
    get.mockResolvedValue(listResult([{ ...providersState()[0], can_refresh_models: true }]))
    render(<ServerProvidersCard />)

    expect(await screen.findByText('Verified')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Store .* API key/ })).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Refresh gemini models' })).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: 'Edit gemini key' })).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: 'Remove gemini' })).toHaveLength(1)
  })

  it('shows neither Store nor key controls on a provider without key support', async () => {
    get.mockResolvedValue(listResult([deploymentEntry('vertex-claude', false)]))
    render(<ServerProvidersCard />)

    expect(await screen.findByText('Deployment credentials')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Store .* API key/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit vertex-claude key' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove vertex-claude' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Refresh vertex-claude models' })).not.toBeInTheDocument()
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
    await user.click(await screen.findByRole('button', { name: 'Edit gemini key' }))
    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(within(dialog).getByLabelText('New API key'), 'new-secret')
    refetched = true
    await user.click(within(dialog).getByRole('button', { name: 'Save key' }))
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

describe('ServerProvidersCard remove and key dialogs', () => {
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

  it('opens the edit dialog empty and posts the rotation body (#378)', async () => {
    const user = userEvent.setup()
    render(<ServerProvidersCard />)
    const dialog = await openEditDialog(user)

    const modelField = within(dialog).getByRole('combobox', { name: 'Verification model' })
    const keyField = within(dialog).getByLabelText('New API key')
    const save = within(dialog).getByRole('button', { name: 'Save key' })
    // No default model, no autofocused dropdown, no key carried over
    expect(modelField).toHaveValue('')
    expect(keyField).toHaveValue('')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(save).toBeDisabled()

    await user.type(modelField, 'gemini-2.5-pro')
    expect(save).toBeDisabled()
    await user.type(keyField, 'new-secret')
    expect(save).toBeEnabled()
    await user.click(save)

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/api/admin/server-providers/gemini/rotate-key', {
        provider: 'gemini',
        api_key: 'new-secret', // pragma: allowlist secret
        verification_model: 'gemini-2.5-pro',
      }),
    )
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(await screen.findByRole('status')).toHaveTextContent('API key verified and rotated.')
    expect(get).toHaveBeenCalledTimes(2)
  })

  it('reports an ok:false rotation without closing the dialog (#378)', async () => {
    const user = userEvent.setup()
    post.mockResolvedValueOnce({ ok: false, outcome: 'rejected' } as never)
    render(<ServerProvidersCard />)
    const dialog = await openEditDialog(user)

    await user.type(within(dialog).getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(within(dialog).getByLabelText('New API key'), 'bad-secret')
    await user.click(within(dialog).getByRole('button', { name: 'Save key' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Key rejected')
    // Still editing: the dialog stays open on a rejected key
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('reopens the edit dialog with an empty key field (#378)', async () => {
    const user = userEvent.setup()
    render(<ServerProvidersCard />)
    const dialog = await openEditDialog(user)

    await user.type(within(dialog).getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(within(dialog).getByLabelText('New API key'), 'new-secret')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    const reopened = await openEditDialog(user)
    expect(within(reopened).getByRole('combobox', { name: 'Verification model' })).toHaveValue('')
    expect(within(reopened).getByLabelText('New API key')).toHaveValue('')
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })

  it('hints Edit key with what it does (#378)', async () => {
    const user = userEvent.setup()
    render(<ServerProvidersCard />)

    await user.hover(await screen.findByRole('button', { name: 'Edit gemini key' }))
    expect(await screen.findByRole('tooltip')).toHaveTextContent('Replaces the stored API key')
  })
})
