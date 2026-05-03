import { useCallback } from 'react'
import { useSessionState } from './useSessionState'
import type { SortDirection } from '@/components/shared/SortableHeader'

/**
 * Shared table sort state with sessionStorage persistence.
 * @param storagePrefix - unique prefix for sessionStorage keys
 * @param defaultKey - default sort column key
 * @param defaultDir - default sort direction
 * @param descDefaultKeys - column keys that default to 'desc' when first clicked
 */
export function useTableSort(
  storagePrefix: string,
  defaultKey: string,
  defaultDir: SortDirection,
  descDefaultKeys: string[] = [],
) {
  const [sortKey, setSortKey] = useSessionState(`${storagePrefix}.sortKey`, defaultKey)
  const [sortDir, setSortDir] = useSessionState<SortDirection>(`${storagePrefix}.sortDir`, defaultDir)

  const handleSort = useCallback(
    (key: string) => {
      if (key === sortKey) {
        setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
      } else {
        setSortKey(key)
        setSortDir(descDefaultKeys.includes(key) ? 'desc' : 'asc')
      }
    },
    [sortKey, sortDir, setSortKey, setSortDir, descDefaultKeys],
  )

  return { sortKey, sortDir, handleSort }
}
