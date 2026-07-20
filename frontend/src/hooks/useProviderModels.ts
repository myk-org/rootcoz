import { useState, useEffect, useCallback, useRef } from 'react'
import type { ModelOption } from '@/components/shared/ModelCombobox'
import { normalizeProvider } from '@/lib/aiProviders'
import { fetchModelsForProvider } from '@/lib/useProviderModels'
import { api } from '@/lib/api'

/**
 * Provider models with admin refresh support.
 * Reuses lib/fetchModelsForProvider for the shared fetch path.
 */
export function useProviderModels(provider: string) {
  const [availableModels, setAvailableModels] = useState<ModelOption[]>([])
  const [refreshing, setRefreshing] = useState(false)
  const normalized = normalizeProvider(provider)
  const providerRef = useRef(normalized)
  const refreshSeqRef = useRef(0)

  useEffect(() => {
    providerRef.current = normalized
  }, [normalized])

  useEffect(() => {
    if (!normalized) {
      setAvailableModels([])
      return
    }
    let ignore = false
    setAvailableModels([])
    fetchModelsForProvider(normalized)
      .then((models) => {
        if (!ignore) setAvailableModels(models)
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
      if (
        currentProvider &&
        providerRef.current === currentProvider &&
        refreshSeqRef.current === seq
      ) {
        const models = await fetchModelsForProvider(currentProvider)
        if (providerRef.current === currentProvider && refreshSeqRef.current === seq) {
          setAvailableModels(models)
        }
      }
    } catch (err) {
      console.error('Failed to refresh AI models:', err)
    } finally {
      if (refreshSeqRef.current === seq) {
        setRefreshing(false)
      }
    }
  }, [])

  return { models: availableModels, refresh, refreshing }
}
