import { useEffect, useState } from 'react'
import { api } from '@/lib/api'

/**
 * Distinct submitters for the filter bar (dashboard and reports, one source). Paged rows cannot supply them,
 * so the server enumerates. A failed or empty response leaves the list empty —
 * the picker is free-entry, so the filter stays usable either way.
 */
export function useSubmitters(): { submitters: string[]; loading: boolean } {
  const [submitters, setSubmitters] = useState<string[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    api.get<{ submitters?: string[] }>('/api/dashboard/submitters')
      .then((data) => {
        if (cancelled) return
        // No dedupe here: the server already collapsed names with SQLite's
        // ASCII-only lower(), and folding again in JS (full Unicode) would drop
        // a name the server deliberately kept — and the picker could then never
        // offer it, since it treats a case-folded match as already listed.
        setSubmitters(Array.isArray(data?.submitters) ? data.submitters : [])
        setLoading(false)
      })
      .catch(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [])

  return { submitters, loading }
}
