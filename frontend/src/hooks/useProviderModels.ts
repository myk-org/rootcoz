import { useState, useEffect, useCallback, useRef } from 'react'
import { api } from '@/lib/api'
import type { ModelOption } from '@/components/shared/ModelCombobox'
import { normalizeProvider } from '@/lib/aiProviders'

export function useProviderModels(provider: string) {
  const [availableModels, setAvailableModels] = useState<ModelOption[]>([])
  const [refreshing, setRefreshing] = useState(false)
  const normalized = normalizeProvider(provider)
  const providerRef = useRef(normalized)
  const refreshSeqRef = useRef(0)

  // Keep provider ref in sync
  useEffect(() => { providerRef.current = normalized }, [normalized])

  useEffect(() => {
    if (!normalized) {
      setAvailableModels([])
      return
    }
    let ignore = false
    setAvailableModels([])
    api
      .get<{ models: ModelOption[] }>(`/api/ai-models?provider=${encodeURIComponent(normalized)}`)
      .then((res) => {
        if (!ignore) setAvailableModels(res.models ?? [])
      })
      .catch(() => {
        if (!ignore) setAvailableModels([])
      })
    return () => {
      ignore = true
    }
  }, [normalized])

  const refresh = useCallback(async () => {
    const seq = ++refreshSeqRef.current
    const currentProvider = providerRef.current
    setRefreshing(true)
    try {
      await api.post('/api/admin/ai-models/refresh')
      // Only apply results if provider hasn't changed and no newer refresh started
      if (currentProvider && providerRef.current === currentProvider && refreshSeqRef.current === seq) {
        const res = await api.get<{ models: ModelOption[] }>(
          `/api/ai-models?provider=${encodeURIComponent(currentProvider)}`,
        )
        if (providerRef.current === currentProvider && refreshSeqRef.current === seq) {
          setAvailableModels(res.models ?? [])
        }
      }
    } catch (err) {
      console.error('Failed to refresh AI models:', err)
    } finally {
      // Only clear refreshing if no newer refresh started
      if (refreshSeqRef.current === seq) {
        setRefreshing(false)
      }
    }
  }, [])

  return { models: availableModels, refresh, refreshing }
}
