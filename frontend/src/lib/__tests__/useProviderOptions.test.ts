import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  _resetProviderCatalogCacheForTests,
  resetProviderCatalogCache,
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

    expect(getMock).toHaveBeenCalledTimes(1)
    expect(getMock).toHaveBeenCalledWith('/api/ai-models')

    await act(async () => {
      resolveGet({
        providers: {
          openai: [{ id: 'gpt-5', name: 'GPT-5' }],
          claude: [],
          gemini: [],
        },
        provider_status: {
          openai: { has_api_key: true, modelListingSupported: true, model_count: 1 },
        },
      })
    })

    await waitFor(() => {
      expect(a.result.current.enabled).toEqual(['openai'])
      expect(b.result.current).toEqual(['openai'])
      expect(a.result.current.providerStatus.openai?.has_api_key).toBe(true)
    })

    expect(getMock).toHaveBeenCalledTimes(1)
    a.unmount()
    b.unmount()
  })

  it('normalizes mixed-case provider IDs so lookups via normalizeProvider resolve', async () => {
    getMock.mockImplementation(async () => ({
      providers: {
        EnMaaS: [{ id: 'rits/zai-org/glm-5-3', name: 'GLM 5.3 (EnMaaS)', credential_sources: ['server'], verified: true }],
        openai: [{ id: 'gpt-5', credential_sources: ['server'], verified: true }],
      },
      provider_status: {},
    }))
    const { result } = renderHook(() => useProviderCatalog())
    await waitFor(() => expect(result.current.providerKeys).toEqual(['enmaas', 'openai']))
    // Consumers look providers up lowercased (normalizeProvider); the catalog
    // must be keyed to match or the provider is invisible in pickers.
    expect(result.current.providers.enmaas).toHaveLength(1)
    expect(result.current.enabled).toEqual(['enmaas', 'openai'])
  })

  it('drops case-distinct provider variants instead of overwriting one with the other', async () => {
    getMock.mockImplementation(async () => ({
      providers: {
        Foo: [{ id: 'foo-model', credential_sources: ['server'], verified: true }],
        foo: [{ id: 'other-model', credential_sources: ['server'], verified: true }],
        openai: [{ id: 'gpt-5', credential_sources: ['server'], verified: true }],
      },
      provider_status: {
        Foo: { has_api_key: true, modelListingSupported: true },
        foo: { has_api_key: true, modelListingSupported: false },
      },
    }))
    const { result } = renderHook(() => useProviderCatalog())
    // Ambiguous case-variant IDs are unusable downstream (the backend
    // refuses ambiguous case-insensitive matches) — hide both rather than
    // letting one silently win.
    await waitFor(() => expect(result.current.providerKeys).toEqual(['openai']))
    expect(result.current.providers.foo).toBeUndefined()
    expect(result.current.providerStatus.foo).toBeUndefined()
  })

  it('reports an empty catalog as loaded, but not a failed request', async () => {
    // An answer with no providers is an answer; a failed request says nothing.
    getMock.mockImplementation(async () => ({ providers: {}, provider_status: {} }))
    const empty = renderHook(() => useProviderCatalog())
    await waitFor(() => expect(empty.result.current.settled).toBe(true))
    expect(empty.result.current.loaded).toBe(true)
    expect(empty.result.current.providerKeys).toEqual([])
    empty.unmount()

    _resetProviderCatalogCacheForTests()
    getMock.mockReset()
    getMock.mockRejectedValue(new Error('503 Service Unavailable'))
    const failed = renderHook(() => useProviderCatalog())
    await waitFor(() => expect(failed.result.current.settled).toBe(true))
    expect(failed.result.current.loaded).toBe(false)
    failed.unmount()
  })

  it('caches the catalog for later mounts and refetches after a reset', async () => {
    getMock.mockImplementation(async () => ({ providers: { openai: [{ id: 'gpt' }] } }))
    const regular = renderHook(() => useProviderCatalog())
    await waitFor(() => expect(regular.result.current.providerKeys).toEqual(['openai']))
    expect(getMock).toHaveBeenCalledTimes(1)
    const again = renderHook(() => useProviderCatalog())
    expect(again.result.current.providerKeys).toEqual(['openai'])
    expect(getMock).toHaveBeenCalledTimes(1)
    act(() => resetProviderCatalogCache())
    await waitFor(() => expect(getMock).toHaveBeenCalledTimes(2))
    regular.unmount()
    again.unmount()
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

  it('does not add status-only providers to provider options', async () => {
    getMock.mockResolvedValue({
      providers: { openai: [{ id: 'gpt-5', name: 'GPT 5' }] },
      provider_status: { openrouter: { has_api_key: true, modelListingSupported: false } },
    })
    const { result } = renderHook(() => useProviderOptions())

    await waitFor(() => expect(result.current.map((option) => option.value)).toEqual(['openai']))
  })

  it('refetches mounted catalog consumers after resetProviderCatalogCache', async () => {
    getMock
      .mockResolvedValueOnce({
        providers: {
          openai: [{ id: 'gpt-5', name: 'GPT-5' }],
          claude: [],
          gemini: [],
        },
        provider_status: {
          openai: { has_api_key: true, modelListingSupported: false },
        },
      })
      .mockResolvedValueOnce({
        providers: {
          openai: [{ id: 'gpt-5', name: 'GPT-5' }],
          claude: [{ id: 'claude:1', name: 'Sonnet' }],
          gemini: [],
        },
        provider_status: {
          openai: { has_api_key: true, modelListingSupported: true, model_count: 1 },
        },
      })

    const { result, unmount } = renderHook(() => useProviderCatalog())

    await waitFor(() => {
      expect(result.current.enabled).toEqual(['openai'])
      expect(result.current.providerStatus.openai?.modelListingSupported).toBe(false)
    })
    expect(getMock).toHaveBeenCalledTimes(1)

    await act(async () => {
      resetProviderCatalogCache()
    })

    await waitFor(() => {
      expect(result.current.enabled).toEqual(
        expect.arrayContaining(['openai', 'claude']),
      )
      expect(result.current.enabled).toHaveLength(2)
      expect(result.current.providerStatus.openai?.modelListingSupported).toBe(true)
    })
    expect(getMock).toHaveBeenCalledTimes(2)
    unmount()
  })
})
