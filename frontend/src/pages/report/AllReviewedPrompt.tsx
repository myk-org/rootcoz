import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
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
import { resolveBuildDisplayId } from '@/lib/utils'
import { exporterEnabled, useReportState } from '@/pages/report/reportState'
import { pushRejectionDetail } from '@/lib/pushGate'
import { defaultedNotice, unmatchedFailure } from '@/lib/pushResult'
import { pushFullySucceeded, pushToExporter } from './useExporterPush'
import { GreenwavePushForm } from './GreenwavePushForm'

interface AllReviewedPromptProps {
  jobId: string
}

/** Minimal shape of a push response, used to detect failed pushes. */
interface PushResponse {
  success?: boolean
  message?: string
  errors?: string[]
  pushed?: number
  unmatched?: string[]
  defaulted?: string[]
}

/**
 * Extract a user-facing failure detail from a push response, or null when
 * the push succeeded. Covers the generic exporter response
 * (`success`/`message`) and the Report Portal response (`errors`,
 * `unmatched` — a test that was not pushed). `defaulted` is not a failure:
 * those tests were pushed under the TO INVESTIGATE fallback, so it is carried
 * separately by `pushDefaultedItems` and reported as a notice, never an error.
 */
function pushFailureDetail(
  res: unknown,
): { message: string; detail?: string } | null {
  if (!res || typeof res !== 'object') return null
  const r = res as PushResponse
  if (r.success === false) {
    return { message: r.message || r.errors?.[0] || 'push failed' }
  }
  if (r.errors && r.errors.length > 0) return { message: r.errors[0] }
  if (r.unmatched && r.unmatched.length > 0) {
    return unmatchedFailure(r.pushed ?? 0, r.unmatched)
  }
  return null
}

/** Tests a response pushed under TO INVESTIGATE — empty when none. */
function pushDefaultedItems(res: unknown): string[] {
  if (!res || typeof res !== 'object') return []
  const r = res as PushResponse
  if (r.success === false) return []
  return r.defaulted ?? []
}

export function AllReviewedPrompt({ jobId }: AllReviewedPromptProps) {
  const { result, reviews, reportportalAvailable, datadogAvailable, exporters } =
    useReportState()
  const { isOperator } = useAuth()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [pushing, setPushing] = useState(false)
  const [pushError, setPushError] = useState<string | null>(null)
  // Muted second line under the error — the unmatched entries, which name the
  // classification the backend could not map
  const [pushErrorDetail, setPushErrorDetail] = useState<string | null>(null)
  // The push went through, but tests landed under TO INVESTIGATE — the dialog
  // stays open to say so instead of closing as if everything was classified
  const [pushNotice, setPushNotice] = useState<{
    message: string
    detail: string
  } | null>(null)
  // Report Portal / Datadog decision, so collapsing their block never
  // dismisses the Greenwave offer sitting in the same prompt.
  const [eventPushStatus, setEventPushStatus] = useState<
    'pushed' | 'failed' | 'skipped' | null
  >(null)
  // Whether the Greenwave section has completed a push: the two offers share
  // one dialog, so neither may dismiss the other while it is still unanswered.
  const [greenwaveDone, setGreenwaveDone] = useState(false)
  // Push combinations (`exporter + scope query`) that already succeeded in
  // the current dialog session — a retry must only re-push the failed ones,
  // because Datadog events are created per POST.
  const succeededRef = useRef<Set<string>>(new Set())
  // Invalidates in-flight push runs: navigation resets the prompt, so a
  // late completion from an old job must not write state (same guard the
  // page uses for test-result generations).
  const pushRunRef = useRef(0)
  // Monotonic Greenwave dialog-session id. Bumped wherever the section
  // restarts (navigation, fresh prompt, dialog close): a push that lands
  // after that point belongs to a dead session and must not mark the live
  // one complete, or its form would vanish unanswered.
  const greenwaveSessionRef = useRef(0)

  /** Restart the Greenwave section: forget any completed push. */
  const resetGreenwaveSession = useCallback(() => {
    greenwaveSessionRef.current++
    setGreenwaveDone(false)
  }, [])

  const greenwaveEnabled = exporterEnabled(exporters, 'greenwave')
  // The Greenwave section pushes the root result only, so it is not offered
  // for a pipeline whose failures all live in child jobs.
  const hasRootFailures = (result?.failures ?? []).length > 0
  const greenwaveOffered = greenwaveEnabled && hasRootFailures
  const eventExportersOffered = reportportalAvailable || datadogAvailable
  // The Greenwave session the form rendered below belongs to, captured during
  // render so a callback arriving after a reset can tell it is stale.
  const gwSession = greenwaveSessionRef.current

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
  // What is actually offered, not merely enabled: a child-only pipeline
  // has nothing for the root-scope Greenwave push.
  greenwaveRef.current = greenwaveOffered
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
          // Fresh dialog session: reset the whole outcome, not just the
          // succeeded-combination set — a stale "… updated." line would
          // otherwise replace the Yes/No buttons and dead-end the prompt.
          // The run counter is bumped too: a push still in flight from the
          // previous session must not land its outcome here (same guard the
          // navigation reset uses).
          succeededRef.current = new Set()
          pushRunRef.current++
          setEventPushStatus(null)
          setPushError(null)
          setPushErrorDetail(null)
          setPushNotice(null)
          resetGreenwaveSession()
          setDialogOpen(true)
        }
      })
    }
    window.addEventListener('rootcoz:review-changed', onReviewChanged)
    return () => window.removeEventListener('rootcoz:review-changed', onReviewChanged)
  }, [jobId, resetGreenwaveSession]) // re-subscribe when jobId changes

  // Reset on navigation
  useEffect(() => {
    setDialogOpen(false)
    setPushing(false)
    setPushError(null)
    setPushErrorDetail(null)
    setPushNotice(null)
    setEventPushStatus(null)
    succeededRef.current = new Set()
    pushRunRef.current++
    resetGreenwaveSession()
  }, [jobId, resetGreenwaveSession]) // re-run when jobId changes

  const handleOpenChange = useCallback(
    (open: boolean) => {
      setDialogOpen(open)
      // A closed dialog must not resurrect a stale error on reopen
      if (!open) {
        setPushError(null)
        setPushErrorDetail(null)
        setPushNotice(null)
        setEventPushStatus(null)
        resetGreenwaveSession()
      }
    },
    [resetGreenwaveSession],
  )

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
    setPushErrorDetail(null)
    setPushNotice(null)
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
          pushes.push({ key, label, promise: pushToExporter(jobId, exporter, scope) })
        }
      }
      const settled = await Promise.allSettled(pushes.map((p) => p.promise))
      if (runId !== pushRunRef.current) return
      const failures: string[] = []
      const details: string[] = []
      const defaulted: string[] = []
      settled.forEach((outcome, i) => {
        const { key, label } = pushes[i]
        if (outcome.status === 'rejected') {
          failures.push(`${label}: ${pushRejectionDetail(outcome.reason)}`)
        } else {
          const detail = pushFailureDetail(outcome.value)
          if (detail) {
            failures.push(`${label}: ${detail.message}`)
            if (detail.detail) details.push(detail.detail)
          } else {
            succeededRef.current.add(key)
            defaulted.push(...pushDefaultedItems(outcome.value))
          }
        }
      })
      if (failures.length > 0) {
        // Keep the dialog open so the user sees what failed
        setPushError(failures.join('; '))
        setPushErrorDetail(details.length > 0 ? details.join('; ') : null)
        setEventPushStatus('failed')
      } else if (defaulted.length > 0) {
        // Pushed, but under the TO INVESTIGATE fallback — say which tests.
        // The question stays answerable: a retry sends nothing new and closes.
        setPushNotice(defaultedNotice(defaulted))
        setEventPushStatus('pushed')
      } else {
        setEventPushStatus('pushed')
        // Only close when there is no Greenwave offer left to answer; the
        // prompt exists to offer both, so one decision must not discard it.
        // Close only when nothing else is still on offer.
        if (!greenwaveOffered || greenwaveDone) handleOpenChange(false)
      }
    } finally {
      if (runId === pushRunRef.current) setPushing(false)
    }
  }

  const handleSkip = () => {
    setPushError(null)
    setPushErrorDetail(null)
    setPushNotice(null)
    setEventPushStatus('skipped')
    if (!greenwaveOffered || greenwaveDone) handleOpenChange(false)
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
              {eventPushStatus !== null && (
                <p className="text-xs text-text-tertiary" role="status">
                  {eventPushStatus === 'pushed' && `${pushTargets} updated.`}
                  {eventPushStatus === 'failed' && `${pushTargets} push failed.`}
                  {eventPushStatus === 'skipped' && `${pushTargets} update skipped.`}
                </p>
              )}
              {/* A failed push keeps the answer editable: retrying only
                  re-sends the combinations that have not succeeded yet. A
                  TO INVESTIGATE notice does too — the tests were pushed, so
                  a retry sends nothing new and closes the prompt. */}
              {(eventPushStatus === null ||
                eventPushStatus === 'failed' ||
                pushNotice !== null) && (
                <div className="flex gap-2">
                  <Button onClick={handleConfirm} disabled={pushing}>
                    {pushing ? 'Loading...' : 'Yes'}
                  </Button>
                  <Button variant="outline" onClick={handleSkip} disabled={pushing}>
                    No
                  </Button>
                </div>
              )}
              {pushError && (
                <p className="text-xs text-signal-red" role="alert">
                  {pushError}
                </p>
              )}
              {pushErrorDetail && (
                <p className="text-xs text-text-tertiary">{pushErrorDetail}</p>
              )}
              {pushNotice && (
                <p className="text-xs text-text-secondary" role="status">
                  {pushNotice.message}
                </p>
              )}
              {pushNotice && (
                <p className="text-xs text-text-tertiary" role="status">
                  {pushNotice.detail}
                </p>
              )}
            </div>
          )}

          {greenwaveEnabled && result && (
            <div className="space-y-3">
              <p className="text-sm text-text-primary">
                Push the reviewed failures to Greenwave
              </p>
              {greenwaveDone ? (
                <p className="text-xs text-text-tertiary" role="status">
                  Greenwave push completed. Answer the remaining exporter question, or
                  close this prompt.
                </p>
              ) : greenwaveOffered ? (
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
                    buildNumber={resolveBuildDisplayId(result) ?? result.build_number}
                    failures={result.failures ?? []}
                    onPushed={(res) => {
                      // A failed or partial push must stay visible in the result view.
                      if (!pushFullySucceeded(res)) return
                      // Only the live session may complete: a push from a
                      // closed dialog (or a previous job) must not hide the
                      // form of the session now on screen.
                      if (gwSession !== greenwaveSessionRef.current) return
                      setGreenwaveDone(true)
                      // The event question may still be unanswered: keep the
                      // dialog so answering it is still possible. A TO
                      // INVESTIGATE notice is also still unread — closing
                      // here would swallow it.
                      if (
                        pushNotice === null &&
                        (!eventExportersOffered ||
                          eventPushStatus === 'pushed' ||
                          eventPushStatus === 'skipped')
                      ) {
                        handleOpenChange(false)
                      }
                    }}
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
