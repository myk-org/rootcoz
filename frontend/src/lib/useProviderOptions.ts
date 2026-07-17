import { useEffect, useMemo, useState } from 'react'
import { api } from '@/lib/api'
import type { AiModelsResponse } from '@/types'
import {
  AI_PROVIDER_OPTIONS,
  buildProviderOptions,
  type AiProviderOption,
} from '@/lib/aiProviders'

/** Provider ids that currently have at least one discovered model. */
export function useEnabledProviders(): string[] {
  const [enabled, setEnabled] = useState<string[]>([])

  useEffect(() => {
    let ignore = false
    api
      .get<AiModelsResponse>('/api/ai-models')
      .then((res) => {
        if (ignore) return
        const providers = res.providers ?? {}
        setEnabled(
          AI_PROVIDER_OPTIONS.map((o) => o.value).filter(
            (p) => (providers[p] ?? []).length > 0,
          ),
        )
      })
      .catch(() => {
        if (!ignore) setEnabled([])
      })
    return () => {
      ignore = true
    }
  }, [])

  return enabled
}

/**
 * Provider dropdown options: only providers that have models.
 * Always keeps `currentValues` visible so an existing selection still renders.
 */
export function useProviderOptions(
  currentValues: string | string[] | undefined = undefined,
): AiProviderOption[] {
  const enabled = useEnabledProviders()
  const currentKey = Array.isArray(currentValues)
    ? currentValues.join('\0')
    : (currentValues ?? '')

  return useMemo(() => {
    const current = currentKey ? currentKey.split('\0') : []
    return buildProviderOptions(enabled, current)
  }, [enabled, currentKey])
}
