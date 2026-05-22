import { useState, useEffect } from 'react'
import { api } from '@/lib/api'
import type { ModelOption } from '@/components/shared/ModelCombobox'

/**
 * Fetches available models for the given AI provider.
 * Returns the models list. Automatically clears models when the provider changes.
 */
export function useProviderModels(provider: string) {
  const [availableModels, setAvailableModels] = useState<ModelOption[]>([])

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

  return availableModels
}
