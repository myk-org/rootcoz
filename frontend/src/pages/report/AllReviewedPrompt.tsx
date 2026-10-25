import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '@/lib/api'
import { collectAllTestKeys } from '@/lib/failureKeys'
import { collectPushScopes, pushScopeQuery } from '@/lib/pushScopes'
import { ConfirmDialog } from '@/components/shared/ConfirmDialog'
import { useAuth } from '@/lib/auth'
import { useReportState } from '@/pages/report/reportState'

interface AllReviewedPromptProps {
  jobId: string
}

/** Minimal shape of a push response, used to detect failed pushes. */
interface PushResponse {
  success?: boolean
  message?: string
  errors?: string[]
}

/**
 * Extract a user-facing failure detail from a push response, or null when
 * the push succeeded. Covers the generic exporter response
 * (`success`/`message`) and the Report Portal response (`errors`).
 */
function pushFailureDetail(res: unknown): string | null {
  if (!res || typeof res !== 'object') return null
  const r = res as PushResponse
  if (r.success === false) return r.message || r.errors?.[0] || 'push failed'
  if (r.errors && r.errors.length > 0) return r.errors[0]
  return null
}

export function AllReviewedPrompt({ jobId }: AllReviewedPromptProps) {
  const { result, reviews, reportportalAvailable, datadogAvailable } = useReportState()
  const { isOperator } = useAuth()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [pushing, setPushing] = useState(false)
  const [pushError, setPushError] = useState<string | null>(null)

  const allKeys = useMemo(
    () =>
      result
        ? collectAllTestKeys(
            result.failures ?? [],
            result.child_job_analyses ?? [],
          )
        : [],
    [result],
  )

  /** Push targets: the parent scope plus every child scope with failures. */
  const scopes = useMemo(
    () =>
      result
        ? collectPushScopes(result.failures ?? [], result.child_job_analyses ?? [])
        : [],
    [result],
  )

  // Keep refs to latest values so the event handler always reads current state
  const reviewsRef = useRef(reviews)
  reviewsRef.current = reviews
  const allKeysRef = useRef(allKeys)
  allKeysRef.current = allKeys
  const rpRef = useRef(reportportalAvailable)
  rpRef.current = reportportalAvailable
  const datadogRef = useRef(datadogAvailable)
  datadogRef.current = datadogAvailable
  const isOperatorRef = useRef(isOperator)
  isOperatorRef.current = isOperator

  useEffect(() => {
    function onReviewChanged(event: Event) {
      const { detail } = event as CustomEvent<{ jobId?: string }>
      if (detail?.jobId !== jobId) return
      // Wait for React to process the state update and re-render
      requestAnimationFrame(() => {
        const currentReviews = reviewsRef.current
        const currentKeys = allKeysRef.current
        if (!isOperatorRef.current) return
        if ((!rpRef.current && !datadogRef.current) || currentKeys.length === 0) return
        const allNowReviewed = currentKeys.every((k) => currentReviews[k]?.reviewed)
        if (allNowReviewed) {
          setDialogOpen(true)
        }
      })
    }
    window.addEventListener('rootcoz:review-changed', onReviewChanged)
    return () => window.removeEventListener('rootcoz:review-changed', onReviewChanged)
  }, [jobId]) // re-subscribe when jobId changes

  // Reset on navigation
  useEffect(() => {
    setDialogOpen(false)
    setPushing(false)
    setPushError(null)
  }, [jobId])

  const handleConfirm = async () => {
    setPushing(true)
    setPushError(null)
    try {
      // One push per scope (parent + each child job with failures) per
      // available exporter — child scopes carry child_job_name +
      // child_build_number query params, same as the push buttons.
      const pushes: Array<{ label: string; promise: Promise<unknown> }> = []
      for (const scope of scopes) {
        const qs = pushScopeQuery(scope)
        const label = scope.childJobName
          ? `${scope.childJobName} #${scope.childBuildNumber}`
          : 'parent job'
        if (rpRef.current) {
          pushes.push({
            label,
            promise: api.post(`/results/${jobId}/push-reportportal${qs}`),
          })
        }
        if (datadogRef.current) {
          pushes.push({
            label,
            promise: api.post(`/results/${jobId}/push/datadog${qs}`),
          })
        }
      }
      const settled = await Promise.allSettled(pushes.map((p) => p.promise))
      const failures: string[] = []
      settled.forEach((outcome, i) => {
        const { label } = pushes[i]
        if (outcome.status === 'rejected') {
          failures.push(`${label}: push request failed`)
        } else {
          const detail = pushFailureDetail(outcome.value)
          if (detail) failures.push(`${label}: ${detail}`)
        }
      })
      if (failures.length > 0) {
        // Keep the dialog open so the user sees what failed
        setPushError(failures.join('; '))
      } else {
        setDialogOpen(false)
      }
    } finally {
      setPushing(false)
    }
  }

  // Push endpoints are operator-only — never prompt other roles
  if (!isOperator || (!reportportalAvailable && !datadogAvailable)) return null

  const pushTargets =
    reportportalAvailable && datadogAvailable
      ? 'Report Portal and Datadog'
      : reportportalAvailable
        ? 'Report Portal'
        : 'Datadog'

  return (
    <ConfirmDialog
      open={dialogOpen}
      onOpenChange={setDialogOpen}
      title="All failures reviewed"
      description={`All failures reviewed. Update ${pushTargets}?`}
      confirmLabel="Yes"
      cancelLabel="No"
      onConfirm={handleConfirm}
      loading={pushing}
      error={pushError}
    />
  )
}
