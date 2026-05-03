import { useState } from 'react'
import { api } from '@/lib/api'
import { getUsername } from '@/lib/cookies'
import { useReportState, useReportDispatch, reviewKey } from './ReportContext'
import { CheckCircle2 } from 'lucide-react'
import { cn } from '@/lib/utils'

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

  async function toggle() {
    setLoading(true)
    setError(null)
    try {
      const res = await api.put<{ status: string; reviewed_by: string }>(`/results/${jobId}/reviewed`, {
        test_name: testName,
        reviewed: !reviewed,
        child_job_name: childJobName ?? '',
        child_build_number: childBuildNumber ?? 0,
      })
      const username = res.reviewed_by ?? getUsername()
      dispatch({
        type: 'SET_REVIEW',
        payload: { key, state: { reviewed: !reviewed, username, updated_at: new Date().toISOString() } },
      })
      // Notify AllReviewedPrompt to check if all failures are now reviewed.
      // Using a custom event avoids useEffect timing issues entirely.
      if (!reviewed) {
        // We just marked as reviewed — schedule check after React processes the dispatch
        setTimeout(() => {
          window.dispatchEvent(new CustomEvent('jji:review-changed', { detail: { jobId } }))
        }, 100)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to toggle review status')
    } finally {
      setLoading(false)
    }
  }

  const tooltipText = reviewed
    ? reviewedBy
      ? `Reviewed by ${reviewedBy} — click to unmark`
      : 'Mark as unreviewed'
    : 'Mark as reviewed'

  return (
    <span className="inline-flex items-center gap-1">
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
        title={tooltipText}
      >
        <CheckCircle2 className="h-3.5 w-3.5" />
        {reviewed ? 'Reviewed' : 'Review'}
        {reviewed && reviewedBy && (
          <span className="text-signal-green/70">by {reviewedBy}</span>
        )}
      </button>
      {error && <span role="alert" className="text-signal-red text-[10px]">{error}</span>}
    </span>
  )
}
