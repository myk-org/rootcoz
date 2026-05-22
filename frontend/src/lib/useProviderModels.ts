import { useState, useEffect } from 'react'
import { api } from '@/lib/api'
import type { ModelOption } from '@/components/shared/ModelCombobox'

export function useProviderModels(provider: string): ModelOption[] {
  const [models, setModels] = useState<ModelOption[]>([])

  useEffect(() => {
    if (!provider) { setModels([]); return }
    let ignore = false
    setModels([])
    api.get<{ models: ModelOption[] }>(`/ai-models?provider=${provider}`)
      .then(res => { if (!ignore) setModels(res.models ?? []) })
      .catch(() => { if (!ignore) setModels([]) })
    return () => { ignore = true }
  }, [provider])

  return models
}
