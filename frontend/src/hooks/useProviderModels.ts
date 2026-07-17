import { useState, useEffect, useCallback } from 'react'
import { api } from '@/lib/api'
import type { ModelOption } from '@/components/shared/ModelCombobox'

export function useProviderModels(provider: string) {
  const [availableModels, setAvailableModels] = useState<ModelOption[]>([])
  const [refreshing, setRefreshing] = useState(false)

  useEffect(() => {
    if (!provider) {
      setAvailableModels([])
      return
    }
    let ignore = false
    setAvailableModels([])
    api
      .get<{ models: ModelOption[] }>(`/api/ai-models?provider=${provider}`)
      .then((res) => {
        if (!ignore) setAvailableModels(res.models ?? [])
      })
      .catch(() => {
        if (!ignore) setAvailableModels([])
      })
    return () => {
      ignore = true
    }
  }, [provider])

  const refresh = useCallback(async () => {
    setRefreshing(true)
    try {
      await api.post('/api/admin/ai-models/refresh')
      // Re-fetch models for current provider after sidecar refresh
      if (provider) {
        const res = await api.get<{ models: ModelOption[] }>(
          `/api/ai-models?provider=${provider}`,
        )
        setAvailableModels(res.models ?? [])
      }
    } catch (err) {
      console.error('Failed to refresh AI models:', err)
    } finally {
      setRefreshing(false)
    }
  }, [provider])

  return { models: availableModels, refresh, refreshing }
}
