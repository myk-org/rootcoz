import { useEffect, useMemo, useState } from 'react'
import { api } from '@/lib/api'
import type { AiModelsResponse, ProviderStatus } from '@/types'
import {
  AI_PROVIDER_OPTIONS,
  buildProviderOptions,
  type AiProviderOption,
} from '@/lib/aiProviders'

export type { ProviderStatus }

/** Provider ids that currently have at least one discovered model. */
export function useEnabledProviders(): string[] {
  const { enabled } = useProviderCatalog()
  return enabled
}

/** Models catalog + provider_status (e.g. cursor auth). */
export function useProviderCatalog(): {
  enabled: string[]
  providerStatus: Record<string, ProviderStatus>
} {
  const [enabled, setEnabled] = useState<string[]>([])
  const [providerStatus, setProviderStatus] = useState<Record<string, ProviderStatus>>(
    {},
  )

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
        setProviderStatus(res.provider_status ?? {})
      })
      .catch(() => {
        if (!ignore) {
          setEnabled([])
          setProviderStatus({})
        }
      })
    return () => {
      ignore = true
    }
  }, [])

  return { enabled, providerStatus }
}

/**
 * Provider dropdown options: providers with models, plus current selection,
 * plus providers with a non-ok status (e.g. Cursor auth expired — keep visible).
 */
export function useProviderOptions(
  currentValues: string | string[] | undefined = undefined,
): AiProviderOption[] {
  const { enabled, providerStatus } = useProviderCatalog()
  const currentKey = Array.isArray(currentValues)
    ? currentValues.join('\0')
    : (currentValues ?? '')

  return useMemo(() => {
    const current = currentKey ? currentKey.split('\0') : []
    const keepVisible = Object.entries(providerStatus)
      .filter(([, st]) => st && st.ok === false)
      .map(([id]) => id)
    return buildProviderOptions(enabled, [...current, ...keepVisible])
  }, [enabled, currentKey, providerStatus])
}

/** Cursor auth banner copy when provider_status.cursor.ok === false. */
export function useCursorAuthStatus(): ProviderStatus | null {
  const { providerStatus } = useProviderCatalog()
  const st = providerStatus.cursor
  if (!st || st.ok) return null
  return st
}
