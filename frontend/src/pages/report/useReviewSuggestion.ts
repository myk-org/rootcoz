import { useState, useCallback, useRef } from 'react'
import { api } from '@/lib/api'
import { completeAiPairOverride, useAiSelection } from '@/lib/providerCatalog'
import { useReportState, useReportDispatch } from '@/pages/report/reportState'
import { reviewKey } from '@/lib/reviewKey'
import { putReviewed, scopedReviewState } from './failureUpdates'

/* ------------------------------------------------------------------ */
/*  Hook                                                               */
/* ------------------------------------------------------------------ */

interface UseReviewSuggestionOptions {
  jobId: string
  testName: string
  childJobName?: string
  childBuildNumber?: number
}

export function useReviewSuggestion({ jobId, testName, childJobName, childBuildNumber }: UseReviewSuggestionOptions) {
  const { reviews } = useReportState()
  const dispatch = useReportDispatch()
  // The user's remembered pair; empty = backend falls back to settings then the job's params.
  const { aiProvider, aiModel } = useAiSelection()
  const [showSuggestion, setShowSuggestion] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // maybeSuggest is fire-and-forget, so two analyses can be in flight at once and
  // resolve out of order. Only the newest one may write error/showSuggestion.
  const latestIntent = useRef(0)

  const key = reviewKey(testName, childJobName, childBuildNumber)
  const isAlreadyReviewed = reviews[key]?.reviewed ?? false

  /** Call after a comment is added. Asks the backend whether the comment implies the failure has been reviewed. */
  const maybeSuggest = useCallback(
    async (commentText: string) => {
      if (isAlreadyReviewed) return
      const requestId = ++latestIntent.current
      // A stale failure from an earlier comment must not survive into this analysis.
      setError(null)
      try {
        const res = await api.post<{ suggests_reviewed: boolean; reason: string }>(
          '/api/analyze-comment-intent',
          {
            comment: commentText,
            job_id: jobId,
            ...completeAiPairOverride(aiProvider, aiModel),
          },
        )
        // Also cleared here: a slower earlier failure can land after this one started.
        if (requestId !== latestIntent.current) return
        setError(null)
        if (res.suggests_reviewed) {
          setShowSuggestion(true)
        }
      } catch (err) {
        if (requestId !== latestIntent.current) return
        // Don't prompt (safe default), but the failure must be visible rather than silent.
        setError(err instanceof Error ? err.message : 'Failed to analyze comment intent')
      }
    },
    [isAlreadyReviewed, jobId, aiProvider, aiModel],
  )

  const dismissSuggestion = useCallback(() => {
    setShowSuggestion(false)
  }, [])

  const confirmSuggestion = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await putReviewed(jobId, { testName, childJobName, childBuildNumber }, true)
      dispatch({
        type: 'SET_REVIEW',
        payload: { key, state: scopedReviewState(true, res.reviewed_by, res.reviewed_at) },
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to mark as reviewed')
    } finally {
      setLoading(false)
      setShowSuggestion(false)
    }
  }, [jobId, testName, childJobName, childBuildNumber, key, dispatch])

  return { showSuggestion, loading, error, maybeSuggest, dismissSuggestion, confirmSuggestion }
}
