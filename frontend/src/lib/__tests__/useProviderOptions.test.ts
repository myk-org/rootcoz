import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  _resetProviderCatalogCacheForTests,
  resetProviderCatalogCache,
  useCursorAuthStatus,
  useEnabledProviders,
  useProviderCatalog,
  useProviderOptions,
} from '@/lib/useProviderOptions'

const getMock = vi.fn()

vi.mock('@/lib/api', () => ({
  api: {
    get: (...args: unknown[]) => getMock(...args),
  },
}))

vi.mock('@/lib/auth', () => ({
  useAuth: () => ({
    username: 'alice',
    isAdmin: false,
    isOperator: false,
    canViewReports: false,
    role: 'reviewer',
    loading: false,
    authenticated: true,
    login: async () => {},
    logout: async () => {},
    refreshAuth: async () => {},
  }),
}))

describe('useProviderCatalog shared fetch', () => {
  afterEach(() => {
    _resetProviderCatalogCacheForTests()
    getMock.mockReset()
  })

  it('shares one /api/ai-models request across concurrent catalog consumers', async () => {
    let resolveGet: (value: unknown) => void = () => {}
    getMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveGet = resolve
        }),
    )

    const a = renderHook(() => useProviderCatalog())
    const b = renderHook(() => useEnabledProviders())
    const c = renderHook(() => useCursorAuthStatus())

    expect(getMock).toHaveBeenCalledTimes(1)
    expect(getMock).toHaveBeenCalledWith('/api/ai-models?force_server_credentials=false')

    await act(async () => {
      resolveGet({
        providers: {
          cursor: [{ id: 'cursor:1', name: 'One' }],
          claude: [],
          gemini: [],
        },
        provider_status: {
          cursor: { ok: false, reason: 'unavailable', hint: 'down' },
        },
      })
    })

    await waitFor(() => {
      expect(a.result.current.enabled).toEqual(['cursor'])
      expect(b.result.current).toEqual(['cursor'])
      expect(c.result.current?.ok).toBe(false)
    })

    expect(getMock).toHaveBeenCalledTimes(1)
    a.unmount()
    b.unmount()
    c.unmount()
  })

  it('reads the Cursor banner status from the same catalog mode as the caller', async () => {
    getMock.mockImplementation(async (path: string) => (path.endsWith('=true')
      ? { providers: {}, provider_status: { cursor: { ok: true } } }
      : { providers: {}, provider_status: { cursor: { ok: false, reason: 'unavailable', hint: 'down' } } }))
    const serverMode = renderHook(() => useCursorAuthStatus(true))
    await waitFor(() => expect(serverMode.result.current).toBeNull())
    expect(getMock).toHaveBeenCalledWith('/api/ai-models?force_server_credentials=true')
    const userMode = renderHook(() => useCursorAuthStatus(false))
    await waitFor(() => expect(userMode.result.current?.ok).toBe(false))
    expect(getMock).toHaveBeenCalledWith('/api/ai-models?force_server_credentials=false')
    serverMode.unmount()
    userMode.unmount()
  })

  it('caches default and forced-server catalogs separately and clears both on reset', async () => {
    getMock.mockImplementation(async (path: string) => ({ providers: path.endsWith('=true') ? { claude: [{ id: 'sonnet' }] } : { openai: [{ id: 'gpt' }] } }))
    const regular = renderHook(() => useProviderCatalog())
    const forced = renderHook(() => useProviderCatalog(true))
    await waitFor(() => expect(regular.result.current.providerKeys).toEqual(['openai']))
    await waitFor(() => expect(forced.result.current.providerKeys).toEqual(['claude']))
    expect(getMock).toHaveBeenCalledTimes(2)
    expect(getMock).toHaveBeenCalledWith('/api/ai-models?force_server_credentials=true')
    const again = renderHook(() => useProviderCatalog(true))
    expect(again.result.current.providerKeys).toEqual(['claude'])
    expect(getMock).toHaveBeenCalledTimes(2)
    act(() => resetProviderCatalogCache())
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(4))
    regular.unmount()
    forced.unmount()
    again.unmount()
  })

  it('requests an unforced catalog when the server default is forced', async () => {
    getMock.mockImplementation(async (path: string) => path === '/api/ai-models?force_server_credentials=false'
      ? { providers: { openai: [{ id: 'gpt', credential_sources: ['user'] }] } }
      : { providers: { claude: [{ id: 'sonnet', credential_sources: ['server'] }] } })
    const { result } = renderHook(() => useProviderCatalog(false))
    await waitFor(() => expect(result.current.providerKeys).toEqual(['openai']))
    expect(getMock).toHaveBeenCalledWith('/api/ai-models?force_server_credentials=false')
  })

  it('ignores a pre-reset response while the refreshed request is pending', async () => {
    const pending: Array<(value: unknown) => void> = []
    getMock.mockImplementation(() => new Promise((resolve) => pending.push(resolve)))
    const { result, unmount } = renderHook(() => useProviderCatalog())
    expect(getMock).toHaveBeenCalledTimes(1)

    await act(async () => {
      resetProviderCatalogCache()
      pending[0]({ providers: { claude: [{ id: 'old' }] } })
      await Promise.resolve()
    })
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(2))
    expect(result.current.providerKeys).toEqual([])

    await act(async () => {
      pending[1]({ providers: { openai: [{ id: 'new' }] } })
    })
    expect(result.current.providerKeys).toEqual(['openai'])
    const second = renderHook(() => useProviderCatalog())
    expect(second.result.current.providerKeys).toEqual(['openai'])
    expect(getMock).toHaveBeenCalledTimes(2)
    second.unmount()
    unmount()
  })

  it('keeps the refreshed catalog when the old response arrives last', async () => {
    const pending: Array<(value: unknown) => void> = []
    getMock.mockImplementation(() => new Promise((resolve) => pending.push(resolve)))
    const { result, unmount } = renderHook(() => useProviderCatalog())
    act(() => resetProviderCatalogCache())
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(2))

    await act(async () => {
      pending[1]({ providers: { openai: [{ id: 'new' }] } })
    })
    expect(result.current.providerKeys).toEqual(['openai'])
    await act(async () => {
      pending[0]({ providers: { claude: [{ id: 'old' }] } })
    })
    expect(result.current.providerKeys).toEqual(['openai'])
    unmount()
  })

  it('does not add diagnostic-only cursor to provider options', async () => {
    getMock.mockResolvedValue({
      providers: { openai: [{ id: 'gpt-5', name: 'GPT 5' }] },
      provider_status: { cursor: { ok: false, reason: 'unavailable', hint: 'down' } },
    })
    const { result } = renderHook(() => useProviderOptions())

    await waitFor(() => expect(result.current.map((option) => option.value)).toEqual(['openai']))
  })

  it('refetches mounted catalog consumers after resetProviderCatalogCache', async () => {
    getMock
      .mockResolvedValueOnce({
        providers: {
          cursor: [{ id: 'cursor:1', name: 'One' }],
          claude: [],
          gemini: [],
        },
        provider_status: {
          cursor: { ok: false, reason: 'unavailable', hint: 'stale' },
        },
      })
      .mockResolvedValueOnce({
        providers: {
          cursor: [{ id: 'cursor:1', name: 'One' }],
          claude: [{ id: 'claude:1', name: 'Sonnet' }],
          gemini: [],
        },
        provider_status: {
          cursor: { ok: true, reason: null, hint: null },
        },
      })

    const { result, unmount } = renderHook(() => useProviderCatalog())

    await waitFor(() => {
      expect(result.current.enabled).toEqual(['cursor'])
      expect(result.current.providerStatus.cursor?.ok).toBe(false)
    })
    expect(getMock).toHaveBeenCalledTimes(1)

    await act(async () => {
      resetProviderCatalogCache()
    })

    await waitFor(() => {
      expect(result.current.enabled).toEqual(
        expect.arrayContaining(['cursor', 'claude']),
      )
      expect(result.current.enabled).toHaveLength(2)
      expect(result.current.providerStatus.cursor?.ok).toBe(true)
    })
    expect(getMock).toHaveBeenCalledTimes(2)
    unmount()
  })
})
