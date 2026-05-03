import { useState, useCallback } from 'react'

export function useExpandCollapseAll(getKeys: () => string[]) {
  const [remountKey, setRemountKey] = useState(0)

  const writeAll = useCallback((expanded: 'true' | 'false') => {
    try {
      for (const key of getKeys()) sessionStorage.setItem(key, expanded)
    } catch { /* sessionStorage may be unavailable */ }
    setRemountKey((k) => k + 1)
  }, [getKeys])

  const expandAll = useCallback(() => writeAll('true'), [writeAll])
  const collapseAll = useCallback(() => writeAll('false'), [writeAll])

  return { remountKey, expandAll, collapseAll }
}
