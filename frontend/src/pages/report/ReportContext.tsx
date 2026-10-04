import { useRef, useCallback, useReducer, type ReactNode } from 'react'
import { api } from '@/lib/api'
import type { CommentEnrichment } from '@/types'
import {
  DispatchCtx,
  RefreshEnrichmentsCtx,
  StateCtx,
  initialState,
  reportReducer,
} from './reportState'

export function ReportProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reportReducer, initialState)
  const enrichmentSeqRef = useRef(0)
  const enrichmentInFlightRef = useRef(false)
  const pendingEnrichmentJobIdRef = useRef<string | null>(null)

  const refreshEnrichments = useCallback((jobId: string) => {
    // Local function (not a self-referencing const) so the queued retry below
    // does not read the callback before its declaration.
    const request = (target: string) => {
      if (enrichmentInFlightRef.current) {
        // Record the latest request so it runs when the current one finishes.
        // Advance the sequence counter to invalidate the current in-flight response
        // so stale data does not overwrite state.
        pendingEnrichmentJobIdRef.current = target
        enrichmentSeqRef.current += 1
        return
      }
      enrichmentInFlightRef.current = true
      pendingEnrichmentJobIdRef.current = null
      const seq = ++enrichmentSeqRef.current
      void api.post<{ enrichments: Record<string, CommentEnrichment[]> }>(`/results/${target}/enrich-comments`)
        .then((res) => {
          if (seq === enrichmentSeqRef.current) {
            dispatch({ type: 'SET_ENRICHMENTS', payload: res.enrichments ?? {} })
          }
        })
        .catch(() => {})
        .finally(() => {
          enrichmentInFlightRef.current = false
          const pending = pendingEnrichmentJobIdRef.current
          if (pending) {
            pendingEnrichmentJobIdRef.current = null
            request(pending)
          }
        })
    }
    request(jobId)
  }, [])

  return (
    <StateCtx.Provider value={state}>
      <DispatchCtx.Provider value={dispatch}>
        <RefreshEnrichmentsCtx.Provider value={refreshEnrichments}>{children}</RefreshEnrichmentsCtx.Provider>
      </DispatchCtx.Provider>
    </StateCtx.Provider>
  )
}
