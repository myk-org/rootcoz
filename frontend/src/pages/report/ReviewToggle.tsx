import { useState } from 'react'
import { useReportState, useReportDispatch } from '@/pages/report/reportState'
import { reviewKey } from '@/lib/reviewKey'
import { putReviewed, notifyReviewChanged, scopedReviewState } from './failureUpdates'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { CheckCircle2 } from 'lucide-react'
import { cn, formatTimestamp, INVALID_DATE_FALLBACK } from '@/lib/utils'

interface ReviewToggleProps {
  jobId: string
  testName: string
  childJobName?: string
  childBuildNumber?: number
  disabled?: boolean
}

export function ReviewToggle({ jobId, testName, childJobName, childBuildNumber, disabled: externalDisabled }: ReviewToggleProps) {
  const { reviews } = useReportState()
  const dispatch = useReportDispatch()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const key = reviewKey(testName, childJobName, childBuildNumber)
  const reviewState = reviews[key]
  const reviewed = reviewState?.reviewed ?? false
  const reviewedBy = reviewState?.username ?? ''
  // The immutable first-review moment; absent on reviews recorded before #566.
  const reviewedAt = reviewState?.reviewed_at ? formatTimestamp(reviewState.reviewed_at) : ''
  // Labelled, never "by <who> on <when>": the marker can be a later user, so the
  // moment is the first review's, not theirs.
  const firstReviewed =
    reviewedAt && reviewedAt !== INVALID_DATE_FALLBACK ? ` · first reviewed ${reviewedAt}` : ''

  async function toggle() {
    setLoading(true)
    setError(null)
    try {
      const res = await putReviewed(jobId, { testName, childJobName, childBuildNumber }, !reviewed)
      dispatch({
        type: 'SET_REVIEW',
        payload: { key, state: scopedReviewState(!reviewed, res.reviewed_by, res.reviewed_at) },
      })
      // Notify AllReviewedPrompt to check if all failures are now reviewed.
      if (!reviewed) notifyReviewChanged(jobId)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to toggle review status')
    } finally {
      setLoading(false)
    }
  }

  const tooltipText = reviewed
    ? reviewedBy
      ? `Reviewed by ${reviewedBy}${firstReviewed} — click to unmark`
      : 'Mark as unreviewed'
    : 'Mark as reviewed'

  return (
    <span className="inline-flex items-center gap-1">
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            aria-pressed={reviewed}
            onClick={(e) => {
              e.stopPropagation()
              toggle()
            }}
            disabled={loading || externalDisabled}
            className={cn(
              'flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium transition-colors duration-150',
              reviewed
                ? 'bg-signal-green/15 text-signal-green'
                : 'bg-surface-elevated text-text-tertiary hover:text-text-secondary',
            )}
          >
            <CheckCircle2 className="h-3.5 w-3.5" />
            {reviewed ? 'Reviewed' : 'Review'}
            {reviewed && reviewedBy && (
              <span className="text-signal-green/70">by {reviewedBy}{firstReviewed}</span>
            )}
          </button>
        </TooltipTrigger>
        <TooltipContent>{tooltipText}</TooltipContent>
      </Tooltip>
      {error && <span role="alert" className="text-signal-red text-[10px]">{error}</span>}
    </span>
  )
}
