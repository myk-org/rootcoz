import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '@/lib/api'
import { collectAllTestKeys, unreviewedKeyCount } from '@/lib/failureKeys'
import { collectPushScopes, pushScopeQuery } from '@/lib/pushScopes'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/lib/auth'
import { exporterEnabled, useReportState } from '@/pages/report/reportState'
import { pushRejectionDetail } from '@/lib/pushGate'
import { GreenwavePushForm } from './GreenwavePushForm'

interface AllReviewedPromptProps {
  jobId: string
}

/** Minimal shape of a push response, used to detect failed pushes. */
interface PushResponse {
  success?: boolean
  message?: string
  errors?: string[]
  unmatched?: string[]
}

/**
 * Extract a user-facing failure detail from a push response, or null when
 * the push succeeded. Covers the generic exporter response
 * (`success`/`message`) and the Report Portal response (`errors`,
 * `unmatched` — a push with unmatched tests recorded nothing, mirroring
 * the ReportPortalButton noop semantics).
 */
function pushFailureDetail(res: unknown): string | null {
  if (!res || typeof res !== 'object') return null
  const r = res as PushResponse
  if (r.success === false) return r.message || r.errors?.[0] || 'push failed'
  if (r.errors && r.errors.length > 0) return r.errors[0]
  if (r.unmatched && r.unmatched.length > 0) {
    return `${r.unmatched.length} test(s) could not be matched to Report Portal items`
  }
  return null
}

export function AllReviewedPrompt({ jobId }: AllReviewedPromptProps) {
  const { result, reviews, reportportalAvailable, datadogAvailable, exporters } =
    useReportState()
  const { isOperator } = useAuth()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [pushing, setPushing] = useState(false)
  const [pushError, setPushError] = useState<string | null>(null)
  // Report Portal / Datadog decision, so collapsing their block never
  // dismisses the Greenwave offer sitting in the same prompt.
  const [eventPushStatus, setEventPushStatus] = useState<
    'pushed' | 'failed' | 'skipped' | null
  >(null)
  // Push combinations (`exporter + scope query`) that already succeeded in
  // the current dialog session — a retry must only re-push the failed ones,
  // because Datadog events are created per POST.
  const succeededRef = useRef<Set<string>>(new Set())
  // Invalidates in-flight push runs: navigation resets the prompt, so a
  // late completion from an old job must not write state (same guard the
  // page uses for test-result generations).
  const pushRunRef = useRef(0)

  const greenwaveEnabled = exporterEnabled(exporters, 'greenwave')
  // The Greenwave section pushes the root result only, so it is not offered
  // for a pipeline whose failures all live in child jobs.
  const hasRootFailures = (result?.failures ?? []).length > 0
  const greenwaveOffered = greenwaveEnabled && hasRootFailures
  const eventExportersOffered = reportportalAvailable || datadogAvailable

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
  const greenwaveRef = useRef(greenwaveEnabled)
  greenwaveRef.current = greenwaveEnabled
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
        if (
          (!rpRef.current && !datadogRef.current && !greenwaveRef.current) ||
          currentKeys.length === 0
        ) {
          return
        }
        const allNowReviewed = unreviewedKeyCount(currentKeys, currentReviews) === 0
        if (allNowReviewed) {
          // Fresh dialog session — previous push outcomes no longer apply
          succeededRef.current = new Set()
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
    setEventPushStatus(null)
    succeededRef.current = new Set()
    pushRunRef.current++
  }, [jobId])

  const handleOpenChange = useCallback((open: boolean) => {
    setDialogOpen(open)
    // A closed dialog must not resurrect a stale error on reopen
    if (!open) {
      setPushError(null)
      setEventPushStatus(null)
    }
  }, [])

  /**
   * Push to Report Portal and/or Datadog: one push per scope (parent + each
   * child job with failures) per available exporter. Child scopes carry
   * child_job_name + child_build_number query params, same as the push
   * buttons. Combos that already succeeded on an earlier attempt of this
   * dialog session are skipped so a retry never duplicates events.
   */
  const handleConfirm = async () => {
    const runId = ++pushRunRef.current
    setPushing(true)
    setPushError(null)
    try {
      const targets: Array<'reportportal' | 'datadog'> = []
      if (rpRef.current) targets.push('reportportal')
      if (datadogRef.current) targets.push('datadog')

      const pushes: Array<{ key: string; label: string; promise: Promise<unknown> }> = []
      for (const scope of scopes) {
        const qs = pushScopeQuery(scope)
        const label = scope.childJobName
          ? `${scope.childJobName} #${scope.childBuildNumber}`
          : 'parent job'
        for (const exporter of targets) {
          const key = `${exporter}${qs}`
          if (succeededRef.current.has(key)) continue
          const path =
            exporter === 'reportportal'
              ? `/results/${jobId}/push-reportportal${qs}`
              : `/results/${jobId}/push/datadog${qs}`
          pushes.push({ key, label, promise: api.post(path) })
        }
      }
      const settled = await Promise.allSettled(pushes.map((p) => p.promise))
      if (runId !== pushRunRef.current) return
      const failures: string[] = []
      settled.forEach((outcome, i) => {
        const { key, label } = pushes[i]
        if (outcome.status === 'rejected') {
          failures.push(`${label}: ${pushRejectionDetail(outcome.reason)}`)
        } else {
          const detail = pushFailureDetail(outcome.value)
          if (detail) failures.push(`${label}: ${detail}`)
          else succeededRef.current.add(key)
        }
      })
      if (failures.length > 0) {
        // Keep the dialog open so the user sees what failed
        setPushError(failures.join('; '))
        setEventPushStatus('failed')
      } else {
        setEventPushStatus('pushed')
        // Only close when there is no Greenwave offer left to answer; the
        // prompt exists to offer both, so one decision must not discard it.
        if (!greenwaveOffered) setDialogOpen(false)
      }
    } finally {
      if (runId === pushRunRef.current) setPushing(false)
    }
  }

  const handleSkip = () => {
    setPushError(null)
    setEventPushStatus('skipped')
    if (!greenwaveOffered) setDialogOpen(false)
  }

  // Push endpoints are operator-only — never prompt other roles
  if (!isOperator || (!eventExportersOffered && !greenwaveEnabled)) return null

  const pushTargets =
    reportportalAvailable && datadogAvailable
      ? 'Report Portal and Datadog'
      : reportportalAvailable
        ? 'Report Portal'
        : 'Datadog'

  return (
    <Dialog open={dialogOpen} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-[560px] bg-surface-card border-border-default">
        <DialogHeader>
          <DialogTitle>All failures reviewed</DialogTitle>
          <DialogDescription>
            Every failure in this result has been reviewed.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-6">
          {eventExportersOffered && (
            <div className="space-y-3">
              <p className="text-sm text-text-primary">
                All failures reviewed. Update {pushTargets}?
              </p>
              {eventPushStatus === null ? (
                <div className="flex gap-2">
                  <Button onClick={handleConfirm} disabled={pushing}>
                    {pushing ? 'Loading...' : 'Yes'}
                  </Button>
                  <Button variant="outline" onClick={handleSkip} disabled={pushing}>
                    No
                  </Button>
                </div>
              ) : (
                <p className="text-xs text-text-tertiary" role="status">
                  {eventPushStatus === 'pushed' && `${pushTargets} updated.`}
                  {eventPushStatus === 'failed' && `${pushTargets} push failed.`}
                  {eventPushStatus === 'skipped' && `${pushTargets} update skipped.`}
                </p>
              )}
              {pushError && (
                <p className="text-xs text-signal-red" role="alert">
                  {pushError}
                </p>
              )}
            </div>
          )}

          {greenwaveEnabled && result && (
            <div className="space-y-3">
              <p className="text-sm text-text-primary">
                Push the reviewed failures to Greenwave
              </p>
              {greenwaveOffered ? (
                <>
                  <p className="text-xs text-text-tertiary">
                    Review and approve the text below before pushing — it states exactly
                    which systems receive the write.
                  </p>
                  {(result.child_job_analyses ?? []).length > 0 && (
                    <p className="text-xs text-text-tertiary">
                      This pushes the root result only. Failures of child jobs are pushed
                      from each child's own row, with their own text.
                    </p>
                  )}
                  <GreenwavePushForm
                    jobId={jobId}
                    jobName={result.job_name}
                    buildNumber={result.build_number}
                    failures={result.failures ?? []}
                    onPushed={() => setDialogOpen(false)}
                  />
                </>
              ) : (
                <p className="text-xs text-text-tertiary">
                  The root result has no failures of its own. Failures of child jobs are
                  pushed from each child's own row, with their own text.
                </p>
              )}
            </div>
          )}
        </div>

        {(greenwaveEnabled || eventExportersOffered) && (
          <DialogFooter>
            <Button variant="outline" onClick={() => handleOpenChange(false)}>
              Close
            </Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  )
}
