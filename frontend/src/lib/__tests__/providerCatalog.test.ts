import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  buildProviderOptions,
  fetchModelsForProvider,
  isAnalysisAiAvailable,
  resetProviderCatalogCache,
  usableModels,
  useCatalogProviderLabels,
  useProviderCatalog,
  useProviderModels,
  visibleModels,
} from '@/lib/providerCatalog'
import type { AiModel, AiModelsResponse, ProviderStatus } from '@/types'

const getMock = vi.fn()
const postMock = vi.fn()

vi.mock('@/lib/api', () => ({
  api: {
    get: (...args: unknown[]) => getMock(...args),
    post: (...args: unknown[]) => postMock(...args),
  },
}))

const authState = {
  username: 'alice',
  isAdmin: true,
  isOperator: true,
  canViewReports: true,
  canUseServerProviders: true,
  canUseFreeProviders: false,
  role: 'admin',
  loading: false,
  authenticated: true,
  login: vi.fn(),
  logout: vi.fn(),
  refreshAuth: vi.fn(),
}

vi.mock('@/lib/auth', () => ({
  useAuth: () => authState,
}))

afterEach(() => {
  act(() => {
    resetProviderCatalogCache()
  })
  getMock.mockReset()
  postMock.mockReset()
  authState.username = 'alice'
  authState.isAdmin = true
  authState.authenticated = true
  authState.canUseServerProviders = true
  authState.canUseFreeProviders = false
})

describe('providerCatalog unified module', () => {
  describe('useProviderModels admin refresh & race safety', () => {
    it('posts admin refresh, resets shared useProviderCatalog synchronously, and reloads active provider models', async () => {
      let catalogVersion = 1
      getMock.mockImplementation((url: string) => {
        if (url === '/api/ai-models') {
          if (catalogVersion === 1) {
            return Promise.resolve({
              providers: { openai: [{ id: 'gpt-4o', name: 'GPT-4o', provider: 'openai', credential_sources: ['server'] }] },
              provider_status: {},
            } satisfies AiModelsResponse)
          }
          return Promise.resolve({
            providers: {
              openai: [
                { id: 'gpt-4o', name: 'GPT-4o', provider: 'openai', credential_sources: ['server'] },
                { id: 'gpt-5', name: 'GPT-5', provider: 'openai', credential_sources: ['server'] },
              ],
            },
            provider_status: {},
          } satisfies AiModelsResponse)
        }
        if (url === '/api/ai-models?provider=openai') {
          return Promise.resolve({
            models: catalogVersion === 1
              ? [{ id: 'gpt-4o', name: 'GPT-4o' }]
              : [{ id: 'gpt-4o', name: 'GPT-4o' }, { id: 'gpt-5', name: 'GPT-5' }],
          })
        }
        throw new Error(`Unexpected GET ${url}`)
      })
      postMock.mockImplementation(async (url: string) => {
        expect(url).toBe('/api/admin/ai-models/refresh')
        catalogVersion = 2
        return { ok: true }
      })

      const catalogHook = renderHook(() => useProviderCatalog())
      const modelsHook = renderHook(() => useProviderModels('openai'))

      await waitFor(() => expect(catalogHook.result.current.providers.openai).toHaveLength(1))
      await waitFor(() => expect(modelsHook.result.current.models.map((m) => m.id)).toEqual(['gpt-4o']))

      await act(async () => {
        await modelsHook.result.current.refresh()
      })

      expect(postMock).toHaveBeenCalledTimes(1)
      expect(postMock).toHaveBeenCalledWith('/api/admin/ai-models/refresh')
      expect(modelsHook.result.current.refreshing).toBe(false)
      expect(modelsHook.result.current.models.map((m) => m.id)).toEqual(['gpt-4o', 'gpt-5'])
      await waitFor(() =>
        expect(catalogHook.result.current.providers.openai?.map((m) => m.id)).toEqual(['gpt-4o', 'gpt-5']),
      )
    })

    it('guards against stale refresh writes when switching providers mid-refresh', async () => {
      let resolveRefreshPost!: () => void
      getMock.mockImplementation((url: string) => {
        if (url === '/api/ai-models?provider=openai') {
          return Promise.resolve({ models: [{ id: 'gpt-4o', name: 'GPT-4o' }] })
        }
        if (url === '/api/ai-models?provider=gemini') {
          return Promise.resolve({ models: [{ id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro' }] })
        }
        throw new Error(`Unexpected GET ${url}`)
      })
      postMock.mockImplementation(
        () => new Promise<void>((resolve) => {
          resolveRefreshPost = resolve
        }),
      )

      const { result, rerender } = renderHook(
        ({ provider }: { provider: string }) => useProviderModels(provider),
        { initialProps: { provider: 'openai' } },
      )

      await waitFor(() => expect(result.current.models.map((m) => m.id)).toEqual(['gpt-4o']))

      let refreshPromise!: Promise<void>
      act(() => {
        refreshPromise = result.current.refresh()
      })
      expect(result.current.refreshing).toBe(true)

      // Switch provider while refresh POST is still in flight.
      rerender({ provider: 'gemini' })
      // Synchronously empties previous provider's models before gemini loads.
      expect(result.current.models).toEqual([])
      await waitFor(() => expect(result.current.models.map((m) => m.id)).toEqual(['gemini-2.5-pro']))

      await act(async () => {
        resolveRefreshPost()
        await refreshPromise
      })

      expect(result.current.refreshing).toBe(false)
      // Must NOT have refetched openai or overwritten gemini's models.
      expect(result.current.models.map((m) => m.id)).toEqual(['gemini-2.5-pro'])
      const openaiCalls = getMock.mock.calls.filter(([url]) => url === '/api/ai-models?provider=openai')
      expect(openaiCalls).toHaveLength(1)
    })

    it('drops stale refresh results even when switching A -> B -> A while refresh is in flight', async () => {
      let resolvePost!: () => void
      let openaiCallCount = 0
      getMock.mockImplementation((url: string) => {
        if (url === '/api/ai-models?provider=openai') {
          openaiCallCount++
          return Promise.resolve({
            models: [{ id: `gpt-v${openaiCallCount}`, name: `GPT v${openaiCallCount}` }],
          })
        }
        if (url === '/api/ai-models?provider=gemini') {
          return Promise.resolve({ models: [{ id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro' }] })
        }
        throw new Error(`Unexpected GET ${url}`)
      })
      postMock.mockImplementation(() => new Promise<void>((resolve) => { resolvePost = resolve }))

      const { result, rerender } = renderHook(
        ({ provider }: { provider: string }) => useProviderModels(provider),
        { initialProps: { provider: 'openai' } },
      )
      await waitFor(() => expect(result.current.models.map((m) => m.id)).toEqual(['gpt-v1']))

      let refreshPromise!: Promise<void>
      act(() => {
        refreshPromise = result.current.refresh()
      })

      rerender({ provider: 'gemini' })
      await waitFor(() => expect(result.current.models.map((m) => m.id)).toEqual(['gemini-2.5-pro']))
      rerender({ provider: 'openai' })
      await waitFor(() => expect(result.current.models.map((m) => m.id)).toEqual(['gpt-v2']))

      await act(async () => {
        resolvePost()
        await refreshPromise
      })

      // Refresh captured providerSeq from before A -> B -> A, so it does not trigger an extra stale reload.
      expect(result.current.refreshing).toBe(false)
      expect(result.current.models.map((m) => m.id)).toEqual(['gpt-v2'])
      expect(openaiCallCount).toBe(2)
    })
  })

  describe('mixed-case custom provider (EnMaaS) round-trip', () => {
    it('preserves exact casing across catalog, labels, options, availability, and per-provider listing', async () => {
      const enmaasModel: AiModel = {
        id: 'enmaas/model-1',
        name: 'Model 1',
        provider: 'EnMaaS',
        credential_sources: ['server'],
        verified: true,
      }
      getMock.mockImplementation((url: string) => {
        if (url === '/api/ai-models') {
          return Promise.resolve({
            providers: {
              EnMaaS: [enmaasModel],
              google: [{ id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', provider: 'google', credential_sources: ['server'] }],
            },
            provider_status: {
              EnMaaS: { has_api_key: true, modelListingSupported: true },
            },
          } satisfies AiModelsResponse)
        }
        if (url === '/api/ai-models?provider=enmaas') {
          return Promise.resolve({ models: [{ id: 'enmaas/model-1', name: 'Model 1' }] })
        }
        throw new Error(`Unexpected GET ${url}`)
      })

      const { result } = renderHook(() => useCatalogProviderLabels(['EnMaaS']))
      await waitFor(() => expect(result.current.loaded).toBe(true))

      // Catalog keys are folded to lowercase while originalIds preserves exact spelling.
      expect(result.current.providerKeys).toEqual(['enmaas', 'google'])
      expect(result.current.originalIds).toEqual({ enmaas: 'EnMaaS', google: 'google' })
      expect(result.current.providerLabels).toEqual({ enmaas: 'EnMaaS', google: 'Gemini' })

      const options = buildProviderOptions(
        result.current.providerKeys,
        ['EnMaaS'],
        result.current.originalIds,
        result.current.providerLabels,
      )
      expect(options).toEqual([
        { value: 'enmaas', label: 'EnMaaS' },
        { value: 'google', label: 'Gemini' },
      ])

      // Mixed-case lookup succeeds in usableModels & isAnalysisAiAvailable.
      expect(usableModels(result.current.providers, 'EnMaaS', false, result.current.providerStatus, true, false)).toHaveLength(1)
      expect(isAnalysisAiAvailable(result.current.providers, result.current.providerStatus, 'EnMaaS', 'enmaas/model-1', false, true, false)).toBe(true)

      // fetchModelsForProvider normalizes mixed-case input before querying.
      const listed = await fetchModelsForProvider('EnMaaS')
      expect(listed).toEqual([{ id: 'enmaas/model-1', name: 'Model 1' }])
      expect(getMock).toHaveBeenCalledWith('/api/ai-models?provider=enmaas')
    })
  })

  describe('grant matrix (server, free, forceServer, personal-key fallback)', () => {
    const models: Record<string, AiModel[]> = {
      openai: [
        { id: 'user-only', name: 'User Only', provider: 'openai', credential_sources: ['user'], verified: true },
        { id: 'server-paid', name: 'Server Paid', provider: 'openai', credential_sources: ['server'], verified: true },
        { id: 'server-free', name: 'Server Free', provider: 'openai', credential_sources: ['server'], verified: true, free: true },
        { id: 'both-sources', name: 'Both Sources', provider: 'openai', credential_sources: ['user', 'server'], verified: true },
      ],
    }
    const status: Record<string, ProviderStatus> = {}

    it.each([
      {
        name: 'no grants, forceServer=false -> personal-key models only',
        forceServer: false, canUseServer: false, canUseFree: false,
        expectedIds: ['user-only', 'both-sources'],
        lockedIds: ['server-paid', 'server-free'],
      },
      {
        name: 'free grant only, forceServer=false -> personal-key + free server models',
        forceServer: false, canUseServer: false, canUseFree: true,
        expectedIds: ['user-only', 'server-free', 'both-sources'],
        lockedIds: ['server-paid'],
      },
      {
        name: 'free grant only, forceServer=true -> free server models only',
        forceServer: true, canUseServer: false, canUseFree: true,
        expectedIds: ['server-free'],
        lockedIds: ['server-paid', 'both-sources'],
      },
      {
        name: 'no grants, forceServer=true -> nothing usable',
        forceServer: true, canUseServer: false, canUseFree: false,
        expectedIds: [],
        lockedIds: ['server-paid', 'server-free', 'both-sources'],
      },
      {
        name: 'server grant, forceServer=false -> all models usable',
        forceServer: false, canUseServer: true, canUseFree: false,
        expectedIds: ['user-only', 'server-paid', 'server-free', 'both-sources'],
        lockedIds: [],
      },
      {
        name: 'server grant, forceServer=true -> all server-backed models usable',
        forceServer: true, canUseServer: true, canUseFree: false,
        expectedIds: ['server-paid', 'server-free', 'both-sources'],
        lockedIds: [],
      },
    ])('$name', ({ forceServer, canUseServer, canUseFree, expectedIds, lockedIds }) => {
      const usable = usableModels(models, 'openai', forceServer, status, canUseServer, canUseFree)
      expect(usable.map((m) => m.id)).toEqual(expectedIds)

      const visible = visibleModels(models, 'openai', forceServer, status, canUseServer, canUseFree)
      expect(visible.filter((m) => m.serverLocked).map((m) => m.id)).toEqual(lockedIds)

      for (const id of expectedIds) {
        expect(isAnalysisAiAvailable(models, status, 'openai', id, forceServer, canUseServer, canUseFree)).toBe(true)
      }
      for (const id of lockedIds) {
        expect(isAnalysisAiAvailable(models, status, 'openai', id, forceServer, canUseServer, canUseFree)).toBe(false)
      }
    })
  })

  describe('useProviderCatalog cache isolation & reset notification', () => {
    it('isolates cached catalogs per auth cacheKey and wakes mounted consumers on reset', async () => {
      let calls = 0
      getMock.mockImplementation(() => {
        calls++
        return Promise.resolve({
          providers: {
            [`prov${calls}`]: [{ id: `m-${calls}`, name: `M ${calls}`, provider: `prov${calls}` }],
          },
        } satisfies AiModelsResponse)
      })

      authState.username = 'alice'
      authState.isAdmin = false
      const { result, rerender } = renderHook(() => useProviderCatalog())
      await waitFor(() => expect(result.current.providerKeys).toEqual(['prov1']))

      // Switch user -> different cacheKey triggers a separate fetch.
      authState.username = 'bob'
      authState.isAdmin = true
      rerender()
      await waitFor(() => expect(result.current.providerKeys).toEqual(['prov2']))

      // Switching back to alice serves alice's cached catalog without refetching.
      authState.username = 'alice'
      authState.isAdmin = false
      rerender()
      expect(result.current.providerKeys).toEqual(['prov1'])
      expect(getMock).toHaveBeenCalledTimes(2)

      // resetProviderCatalogCache wakes the mounted consumer without a cacheKey change.
      act(() => {
        resetProviderCatalogCache()
      })
      await waitFor(() => expect(result.current.providerKeys).toEqual(['prov3']))
      expect(getMock).toHaveBeenCalledTimes(3)
    })
  })
})
