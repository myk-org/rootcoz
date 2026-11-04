import { useEffect, useState } from 'react'
import { api } from '@/lib/api'

/**
 * Distinct submitters for the dashboard filter. Paged rows cannot supply them,
 * so the server enumerates. A failed or empty response leaves the list empty —
 * the picker is free-entry, so the filter stays usable either way.
 */
export function useDashboardSubmitters(): { submitters: string[]; loading: boolean } {
  const [submitters, setSubmitters] = useState<string[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    api.get<{ submitters?: string[] }>('/api/dashboard/submitters')
      .then((data) => {
        if (cancelled) return
        const names = Array.isArray(data?.submitters) ? data.submitters : []
        // One option per name: the server dedupes case-insensitively, keep the
        // first spelling if it ever repeats one.
        const seen = new Set<string>()
        setSubmitters(names.filter((name) => {
          const key = name.toLowerCase()
          if (seen.has(key)) return false
          seen.add(key)
          return true
        }))
        setLoading(false)
      })
      .catch(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [])

  return { submitters, loading }
}
