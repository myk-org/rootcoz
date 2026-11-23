import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import type { FilterFacet, FilterFacetsResponse } from '@/types'

/** Fetches admin-defined tag facets with live per-option match counts. */
export function useFilterFacets(): { facets: FilterFacet[]; loadError: boolean } {
  const [facets, setFacets] = useState<FilterFacet[]>([])
  const [loadError, setLoadError] = useState(false)

  useEffect(() => {
    let cancelled = false
    api
      .get<FilterFacetsResponse>('/api/filters/facets')
      .then((data) => {
        if (cancelled) return
        setLoadError(false)
        setFacets(Array.isArray(data?.facets) ? data.facets : [])
      })
      .catch(() => {
        if (!cancelled) setLoadError(true)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return { facets, loadError }
}
