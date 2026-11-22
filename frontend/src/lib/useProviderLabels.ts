import { useMemo } from 'react'
import { collectProviderIds, disambiguateProviderLabels } from '@/lib/aiProviders'
import { useProviderCatalog } from '@/lib/useProviderOptions'

/**
 * Memoized collision-aware provider label map from explicit catalog IDs plus
 * any additional provider IDs (session, message, peer, or report history).
 */
export function useProviderLabels(
  catalogIds: Iterable<string | null | undefined>,
  extraIds: Iterable<string | null | undefined> = [],
  originalIds?: Readonly<Record<string, string>>,
): Record<string, string> {
  const serializedIds = collectProviderIds(catalogIds, extraIds).join('\0')
  return useMemo(
    () => disambiguateProviderLabels(serializedIds ? serializedIds.split('\0') : [], originalIds),
    [serializedIds, originalIds],
  )
}

/**
 * Live catalog + memoized collision-aware provider labels covering all catalog
 * keys plus any extra provider IDs referenced by the calling view.
 */
export function useCatalogProviderLabels(
  extraIds: Iterable<string | null | undefined> = [],
) {
  const catalog = useProviderCatalog()
  const catalogKeys = catalog.providerKeys ?? Object.keys(catalog.providers ?? {})
  const providerLabels = useProviderLabels(catalogKeys, extraIds, catalog.originalIds)
  return { ...catalog, providerLabels }
}
