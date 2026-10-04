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
  const normalized = normalizeProvider(provider)
  // Models are stored together with the provider they were fetched for, so a
  // provider switch (including clearing it) empties the list synchronously
  // rather than showing the previous provider's models until the fetch lands.
  const [result, setResult] = useState<{ provider: string; models: ModelOption[] }>({ provider: '', models: [] })
  if (result.provider !== normalized) {
    setResult({ provider: normalized, models: [] })
  }

  useEffect(() => {
    if (!normalized) return
    let ignore = false
    fetchModelsForProvider(normalized)
      .then(m => { if (!ignore) setResult({ provider: normalized, models: m }) })
      .catch(() => { if (!ignore) setResult({ provider: normalized, models: [] }) })
    return () => { ignore = true }
  }, [normalized])

  return result.models
}
