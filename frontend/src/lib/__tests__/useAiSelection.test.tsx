import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetProviderCatalogCache, useAiSelection } from '@/lib/providerCatalog'
import { readLastAiPair, writeLastAiPair } from '@/lib/lastAiPair'
import { AnalysisProviderSelect } from '@/components/shared/AnalysisAiPicker'
import { TooltipProvider } from '@/components/ui/tooltip'

const get = vi.fn()

vi.mock('@/lib/api', () => ({ api: { get: (...args: unknown[]) => get(...args) } }))

/** Mutable auth state, so a test can flip the user or the grants on a re-render. */
const auth = {
  username: 'alice',
  isAdmin: false,
  authenticated: true,
  loading: false,
  canUseServerProviders: false,
  canUseFreeProviders: false,
}
vi.mock('@/lib/auth', () => ({ useAuth: () => auth }))

// jsdom lacks pointer capture; the picker's Radix Select calls it on open.
HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

const USER_CATALOG = {
  providers: {
    claude: [{ id: 'sonnet', name: 'Sonnet', provider: 'claude', credential_sources: ['user'] }],
    openai: [{ id: 'gpt-4o', name: 'GPT-4o', provider: 'openai', credential_sources: ['user'] }],
  },
  provider_status: {},
}
const SERVER_CATALOG = {
  providers: { claude: [{ id: 'sonnet', name: 'Sonnet', provider: 'claude', credential_sources: ['server'] }] },
  provider_status: {},
}
/** claude/sonnet is gone: only openai remains. */
const NO_CLAUDE_CATALOG = {
  providers: { openai: [{ id: 'gpt-4o', name: 'GPT-4o', provider: 'openai', credential_sources: ['user'] }] },
  provider_status: {},
}

let catalog: unknown = USER_CATALOG

/** Fallbacks default to a pair this catalog serves; a test overrides them for the rest. */
function Harness({ provider = 'openai', model = 'gpt-4o' }: { provider?: string; model?: string }) {
  const { aiProvider, aiModel, aiPending, setAiPair } = useAiSelection(provider, model)
  return (
    <>
      <span data-testid="pair">{`${aiProvider}/${aiModel}`}</span>
      <span data-testid="settled">{aiPending ? 'pending' : 'settled'}</span>
      <button onClick={() => setAiPair('claude', 'sonnet')}>pick complete</button>
      <button onClick={() => setAiPair('claude', '')}>pick provider</button>
      <button onClick={() => setAiPair('', '')}>pick server default</button>
      <button onClick={() => setAiPair('claude', 'sonn', { commit: false })}>type model</button>
    </>
  )
}

/** The picker a surface renders from the same selection, wrapped as `AnalysisProviderSelect` needs. */
function PickerHarness({ provider = '', model = '' }: { provider?: string; model?: string }) {
  const { aiProvider, setAiPair } = useAiSelection(provider, model)
  return (
    <TooltipProvider>
      <AnalysisProviderSelect value={aiProvider} onChange={(v) => setAiPair(v, '')} forceServer={false} label="AI Provider" />
    </TooltipProvider>
  )
}

/** Renders the harness and waits for the provider catalog to land. */
async function mount(props: { provider?: string; model?: string } = {}) {
  const view = render(<Harness {...props} />)
  await waitFor(() => expect(get).toHaveBeenCalledWith('/api/ai-models'))
  return view
}

const pair = () => screen.getByTestId('pair').textContent
const click = (name: string) => act(() => screen.getByRole('button', { name }).click())
/** Every path the API client was asked for — a model listing for an absent provider is the bug. */
const requestedPaths = () => get.mock.calls.map(([path]) => String(path))

beforeEach(() => {
  auth.username = 'alice'
  auth.authenticated = true
  auth.loading = false
  auth.canUseServerProviders = false
  auth.canUseFreeProviders = false
  catalog = USER_CATALOG
  get.mockReset()
  get.mockImplementation(async () => catalog)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  localStorage.clear()
  act(() => resetProviderCatalogCache())
})

describe('useAiSelection', () => {
  it('falls back to the given defaults when nothing is remembered', async () => {
    await mount()
    await waitFor(() => expect(pair()).toBe('openai/gpt-4o'))
  })

  it('treats a fallback provider the catalog does not serve as no selection', async () => {
    // The stored server default names a provider this server no longer has (#591).
    await mount({ provider: 'removed-provider', model: 'removed-model' })
    await waitFor(() => expect(requestedPaths()).toContain('/api/ai-models'))
    expect(pair()).toBe('/')
    // No model listing may be requested for a provider absent from the catalog —
    // that is the 400 that polluted every mount.
    expect(requestedPaths().some((path) => path.includes('provider='))).toBe(false)
    // Rendering only: the stored default is not rewritten or replaced.
    expect(readLastAiPair('alice')).toBeNull()
  })

  it('offers the catalog providers when the fallback is unavailable, instead of a dead selection', async () => {
    const user = userEvent.setup()
    render(<PickerHarness provider="removed-provider" model="removed-model" />)
    await waitFor(() => expect(requestedPaths()).toContain('/api/ai-models'))
    const trigger = screen.getByRole('combobox', { name: 'AI Provider' })
    // No provider selected: the dead provider's name never reaches the trigger.
    await waitFor(() => expect(trigger).toHaveTextContent('Select provider...'))
    expect(trigger).not.toHaveTextContent('removed-provider')
    await user.click(trigger)
    const options = (await screen.findAllByRole('option')).map((option) => option.textContent).join(' ')
    expect(options).toMatch(/claude/i)
    expect(options).toMatch(/openai/i)
  })

  it('reports the fallback as pending until the catalog judges it', async () => {
    let releaseCatalog: (value: unknown) => void = () => {}
    get.mockImplementation(() => new Promise((resolve) => { releaseCatalog = resolve }))
    render(<Harness />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(screen.getByTestId('settled').textContent).toBe('pending')

    await act(async () => releaseCatalog(USER_CATALOG))
    await waitFor(() => expect(pair()).toBe('openai/gpt-4o'))
    expect(screen.getByTestId('settled').textContent).toBe('settled')
  })

  it('stops waiting when the catalog cannot be fetched, so a surface never locks up', async () => {
    get.mockRejectedValue(new Error('503 Service Unavailable'))
    render(<Harness />)
    await waitFor(() => expect(screen.getByTestId('settled').textContent).toBe('settled'))
    expect(pair()).toBe('/')
  })

  it('keeps the fallback pending while /api/auth/me is still loading, so no empty pair is posted', async () => {
    // The catalog can land before the grants it is judged against: `usable()` rejects a
    // server pair meanwhile, so dropping `aiPending` would enable Continue on '' (#591).
    catalog = SERVER_CATALOG
    auth.loading = true
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {})
    let releaseCatalog: (value: unknown) => void = () => {}
    get.mockImplementation(() => new Promise((resolve) => { releaseCatalog = resolve }))
    const view = render(<Harness provider="claude" model="sonnet" />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    await act(async () => releaseCatalog(SERVER_CATALOG))

    expect(pair()).toBe('/')
    expect(screen.getByTestId('settled').textContent).toBe('pending')
    // The fallback is not judged unusable yet, so it is not announced as ignored.
    expect(debug).not.toHaveBeenCalledWith('[ai-pair] ignored an unavailable default pair', 'claude/sonnet')

    auth.loading = false
    auth.canUseServerProviders = true
    view.rerender(<Harness provider="claude" model="sonnet" />)
    await waitFor(() => expect(pair()).toBe('claude/sonnet'))
    expect(screen.getByTestId('settled').textContent).toBe('settled')
  })

  it('applies a manual model id for a no-listing provider', async () => {
    // The backend keeps the provider in the catalog with an empty model list and reports
    // modelListingSupported: false, so the provider key exists and a manual pair is usable.
    catalog = {
      providers: { openrouter: [] },
      provider_status: { openrouter: { has_api_key: true, modelListingSupported: false } },
    }
    await mount({ provider: 'openrouter', model: 'openrouter/manual-only' })
    await waitFor(() => expect(pair()).toBe('openrouter/openrouter/manual-only'))
    expect(screen.getByTestId('settled').textContent).toBe('settled')
  })

  it('keeps an in-progress draft even when the fallback is unavailable', async () => {
    await mount({ provider: 'removed-provider', model: 'removed-model' })
    await waitFor(() => expect(pair()).toBe('/'))
    await click('pick provider')
    await waitFor(() => expect(pair()).toBe('claude/'))
  })

  it('remembers a committed pair for the next mount of the same user', async () => {
    await mount()
    await click('pick complete')
    await waitFor(() => expect(pair()).toBe('claude/sonnet'))
    cleanup()

    await mount({ provider: 'openai', model: 'gpt-4o' })
    await waitFor(() => expect(pair()).toBe('claude/sonnet'))
  })

  it('does not persist a half pair or an uncommitted model, but keeps it on screen', async () => {
    await mount()
    await click('pick complete')
    await click('pick provider')
    await waitFor(() => expect(pair()).toBe('claude/'))
    expect(readLastAiPair('alice')?.pair).toEqual({ provider: 'claude', model: 'sonnet' })

    // Keystrokes in the model box arrive per character and must not reach the store.
    await click('type model')
    await waitFor(() => expect(pair()).toBe('claude/sonn'))
    expect(readLastAiPair('alice')?.pair).toEqual({ provider: 'claude', model: 'sonnet' })
  })

  it('keeps a completed pick on screen when storage refuses the write', async () => {
    const view = await mount()
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage blocked')
    })
    await click('pick complete')
    await waitFor(() => expect(pair()).toBe('claude/sonnet'))
    // The in-memory value is authoritative, so a re-render must not snap back.
    view.rerender(<Harness />)
    expect(pair()).toBe('claude/sonnet')
  })

  it('treats an explicitly cleared pair as a decision, not as a fallback', async () => {
    await mount()
    await click('pick complete')
    await click('pick server default')
    await waitFor(() => expect(pair()).toBe('/'))
    // The empty pair is stored, so it outranks the fallbacks and survives a remount.
    expect(readLastAiPair('alice')?.pair).toEqual({ provider: '', model: '' })
    cleanup()

    await mount({ provider: 'openai', model: 'gpt-4o' })
    await waitFor(() => expect(pair()).toBe('/'))
  })

  it('stores a new complete pair after the clear, so the clear is not sticky', async () => {
    await mount()
    await click('pick server default')
    await waitFor(() => expect(pair()).toBe('/'))
    await click('pick complete')
    await waitFor(() => expect(readLastAiPair('alice')?.pair).toEqual({ provider: 'claude', model: 'sonnet' }))
    expect(pair()).toBe('claude/sonnet')
  })

  it('drops a remembered pair the catalog no longer offers and falls back', async () => {
    writeLastAiPair('alice', { provider: 'claude', model: 'sonnet' })
    catalog = NO_CLAUDE_CATALOG
    await mount({ provider: 'openai', model: 'gpt-4o' })
    await waitFor(() => expect(readLastAiPair('alice')).toBeNull())
    // A dropped entry means nothing remembered — the surface default applies, not ''.
    await waitFor(() => expect(pair()).toBe('openai/gpt-4o'))
  })

  it('drops a remembered pair the user may no longer use with server credentials', async () => {
    writeLastAiPair('alice', { provider: 'claude', model: 'sonnet' })
    catalog = SERVER_CATALOG
    await mount({ provider: 'claude', model: 'sonnet' })
    await waitFor(() => expect(readLastAiPair('alice')).toBeNull())
    // The default names the same server-backed pair, which this user cannot use either.
    expect(pair()).toBe('/')
  })

  it('never drops a pair while the catalog is empty or failed', async () => {
    writeLastAiPair('alice', { provider: 'claude', model: 'sonnet' })
    get.mockRejectedValue(new Error('catalog unavailable'))
    await mount()
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(readLastAiPair('alice')?.pair).toEqual({ provider: 'claude', model: 'sonnet' })
    expect(pair()).toBe('claude/sonnet')
  })

  it('never drops a cleared pair', async () => {
    writeLastAiPair('alice', { provider: '', model: '' })
    catalog = SERVER_CATALOG
    await mount({ provider: 'openai', model: 'gpt-4o' })
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(readLastAiPair('alice')?.pair).toEqual({ provider: '', model: '' })
    expect(pair()).toBe('/')
  })

  it('waits for /api/auth/me before judging a server-credential pair unusable', async () => {
    writeLastAiPair('alice', { provider: 'claude', model: 'sonnet' })
    catalog = SERVER_CATALOG
    auth.loading = true
    const view = await mount()
    // The catalog may arrive before the grants do — nothing may be erased meanwhile.
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(readLastAiPair('alice')?.pair).toEqual({ provider: 'claude', model: 'sonnet' })

    auth.loading = false
    auth.canUseServerProviders = true
    view.rerender(<Harness />)
    expect(readLastAiPair('alice')?.pair).toEqual({ provider: 'claude', model: 'sonnet' })
    expect(pair()).toBe('claude/sonnet')

    auth.canUseServerProviders = false
    view.rerender(<Harness />)
    await waitFor(() => expect(readLastAiPair('alice')).toBeNull())
  })

  it('never writes under another user key on a username change', async () => {
    writeLastAiPair('alice', { provider: 'claude', model: 'sonnet' })
    const view = await mount()
    await waitFor(() => expect(pair()).toBe('claude/sonnet'))

    auth.username = 'bob'
    view.rerender(<Harness provider="openai" model="gpt-4o" />)
    // Bob sees his own (empty) state; Alice's entry survives untouched.
    await waitFor(() => expect(pair()).toBe('openai/gpt-4o'))
    expect(readLastAiPair('alice')?.pair).toEqual({ provider: 'claude', model: 'sonnet' })
    expect(readLastAiPair('bob')).toBeNull()
  })

  it('re-seeds from the new user key when the username changes in a mounted tree', async () => {
    writeLastAiPair('alice', { provider: 'claude', model: 'sonnet' })
    writeLastAiPair('bob', { provider: 'openai', model: 'gpt-4o' })
    const view = await mount()
    await waitFor(() => expect(pair()).toBe('claude/sonnet'))

    auth.username = 'bob'
    view.rerender(<Harness />)
    await waitFor(() => expect(pair()).toBe('openai/gpt-4o'))
  })
})
