import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { useAiSelection } from '@/lib/useAiSelection'
import { resetProviderCatalogCache } from '@/lib/useProviderOptions'
import { readLastAiPair, writeLastAiPair } from '@/lib/lastAiPair'

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

function Harness({ provider = 'fallback', model = 'fallback-model' }: { provider?: string; model?: string }) {
  const { aiProvider, aiModel, setAiPair } = useAiSelection(provider, model)
  return (
    <>
      <span data-testid="pair">{`${aiProvider}/${aiModel}`}</span>
      <button onClick={() => setAiPair('claude', 'sonnet')}>pick complete</button>
      <button onClick={() => setAiPair('claude', '')}>pick provider</button>
      <button onClick={() => setAiPair('', '')}>pick server default</button>
      <button onClick={() => setAiPair('claude', 'sonn', { commit: false })}>type model</button>
    </>
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
    expect(pair()).toBe('fallback/fallback-model')
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
    expect(pair()).toBe('openai/gpt-4o')
  })

  it('drops a remembered pair the user may no longer use with server credentials', async () => {
    writeLastAiPair('alice', { provider: 'claude', model: 'sonnet' })
    catalog = SERVER_CATALOG
    await mount()
    await waitFor(() => expect(readLastAiPair('alice')).toBeNull())
    expect(pair()).toBe('fallback/fallback-model')
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
