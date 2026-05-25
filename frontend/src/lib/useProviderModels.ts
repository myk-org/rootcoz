import { useState, useEffect } from 'react'
import { api } from '@/lib/api'
import type { ModelOption } from '@/components/shared/ModelCombobox'

/**
 * Fetch available models for a single AI provider.
 * Returns an empty array if `provider` is falsy.
 */
export async function fetchModelsForProvider(provider: string): Promise<ModelOption[]> {
  if (!provider) return []
  const res = await api.get<{ models: ModelOption[] }>(
    `/api/ai-models?provider=${encodeURIComponent(provider)}`,
  )
  return res.models ?? []
}

export function useProviderModels(provider: string): ModelOption[] {
  const [models, setModels] = useState<ModelOption[]>([])

  useEffect(() => {
    if (!provider) { setModels([]); return }
    let ignore = false
    setModels([])
    fetchModelsForProvider(provider)
      .then(m => { if (!ignore) setModels(m) })
      .catch(() => { if (!ignore) setModels([]) })
    return () => { ignore = true }
  }, [provider])

  return models
}
