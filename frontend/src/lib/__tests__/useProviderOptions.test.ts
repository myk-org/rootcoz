import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  _resetProviderCatalogCacheForTests,
  resetProviderCatalogCache,
  useCursorAuthStatus,
  useEnabledProviders,
  useProviderCatalog,
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
    expect(getMock).toHaveBeenCalledWith('/api/ai-models')

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
