import { useState, useEffect } from 'react'
import { api } from '@/lib/api'
import type { ModelOption } from '@/components/shared/ModelCombobox'
import { normalizeProvider } from '@/lib/aiProviders'

/**
 * Fetch available models for a single AI provider.
 * Returns an empty array if `provider` is falsy.
 */
export async function fetchModelsForProvider(provider: string): Promise<ModelOption[]> {
  const normalized = normalizeProvider(provider)
  if (!normalized) return []
  const res = await api.get<{ models: ModelOption[] }>(
    `/api/ai-models?provider=${encodeURIComponent(normalized)}`,
  )
  return res.models ?? []
}

export function useProviderModels(provider: string): ModelOption[] {
  const [models, setModels] = useState<ModelOption[]>([])
  const normalized = normalizeProvider(provider)

  useEffect(() => {
    if (!normalized) { setModels([]); return }
    let ignore = false
    setModels([])
    fetchModelsForProvider(normalized)
      .then(m => { if (!ignore) setModels(m) })
      .catch(() => { if (!ignore) setModels([]) })
    return () => { ignore = true }
  }, [normalized])

  return models
}
