import { useEffect, useRef, useState } from 'react'
import { useAuth } from '@/lib/auth'
import { useReportState, useReportDispatch, reviewKey } from './ReportContext'
import {
  type FailureScope,
  type SelectedGroup,
  getTrackedIn,
  notifyReviewChanged,
  putOverrideClassification,
  putOverridePattern,
  putReviewed,
  putTrackedIn,
  runBatched,
  scopedReviewState,
  selectedScopes,
  trackedInUrlError,
  widenToSignatureGroups,
} from './failureUpdates'
import { detectTrackerType } from './TrackedInBadge'
import { ConfirmDialog } from '@/components/shared/ConfirmDialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { OVERRIDE_CLASSIFICATIONS, PATTERNS } from '@/constants/classifications'
import { CheckCircle2, Link2 } from 'lucide-react'

const BULK_BATCH_SIZE = 5

interface PendingAction {
  title: string
  description: string
  confirmLabel: string
  /** Runs the bulk mutation; resolves with the selection groups that still need
   *  work plus an optional warning about a post-write step that did not land. */
  run: () => Promise<{ failed: SelectedGroup[]; warning?: string }>
}

/** Map failed per-test scopes back to narrowed groups so a retry skips what succeeded. */
function failedGroups(groups: SelectedGroup[], failed: FailureScope[]): SelectedGroup[] {
  const keys = new Set(failed.map((s) => reviewKey(s.testName, s.childJobName, s.childBuildNumber)))
  return groups
    .map((g) => ({ ...g, testNames: g.testNames.filter((n) => keys.has(reviewKey(n, g.childJobName, g.childBuildNumber))) }))
    .filter((g) => g.testNames.length > 0)
}

/** Bulk-apply the existing per-failure edits to the selected failure groups. */
export function BulkUpdateBar() {
  const { result, selection, reviews } = useReportState()
  const dispatch = useReportDispatch()
  const { role } = useAuth()
  const [pending, setPending] = useState<PendingAction | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // A run message that must outlive the bar itself: the selection it applied to is
  // gone, but the user still has to read what did not land.
  const [standaloneError, setStandaloneError] = useState<string | null>(null)
  const [trackOpen, setTrackOpen] = useState(false)
  const [trackUrl, setTrackUrl] = useState('')

  const groups = Object.values(selection)
  // Viewers may read the report but every edit endpoint rejects them.
  const isViewer = role === 'viewer'
  // Latest selection, so a run only clears the scopes it covered — anything the
  // user picked while the requests were in flight survives.
  const selectionRef = useRef(selection)
  selectionRef.current = selection

  // An empty selection means nothing to act on: drop any dialog, stale run scope
  // and error so they cannot resurface on the next selection.
  useEffect(() => {
    if (groups.length === 0) {
      setPending(null)
      setTrackOpen(false)
      setTrackUrl('')
      setError(null)
    } else {
      setStandaloneError(null)
    }
  }, [groups.length])

  if (isViewer) return null
  if (groups.length === 0) {
    return standaloneError ? (
      <div className="pointer-events-none fixed bottom-0 left-0 right-0 z-50 flex justify-center px-6 py-3">
        <span role="alert" className="rounded-md bg-surface-card px-3 py-1.5 text-xs text-signal-red shadow">
          {standaloneError}
        </span>
      </div>
    ) : null
  }
  const jobId = result?.job_id ?? ''
  const scopes = selectedScopes(groups)
  const scopeText = `${scopes.length} ${scopes.length === 1 ? 'test' : 'tests'} in ${groups.length} ${groups.length === 1 ? 'failure' : 'failures'}`
  // An override is applied to the whole error-signature group by the backend, so
  // the scope the user is asked to confirm — and the optimistic patch — must be
  // the full group, not a selection narrowed by an earlier failure.
  const overrideTargets = widenToSignatureGroups(result, groups)
  const overrideScopes = selectedScopes(overrideTargets)
  const overrideScopeText = `every test sharing the error signature — ${overrideScopes.length} ${overrideScopes.length === 1 ? 'test' : 'tests'} in ${overrideTargets.length} ${overrideTargets.length === 1 ? 'failure' : 'failures'}`

  /** Keep only what the run did not finish: untried scopes and the failed ones.
   *  Returns true when nothing is left selected. */
  function keepSelectionAfterRun(covered: FailureScope[], failed: SelectedGroup[]) {
    const coveredKeys = new Set(covered.map((s) => reviewKey(s.testName, s.childJobName, s.childBuildNumber)))
    const failedKeys = new Set(failed.flatMap((g) => g.testNames.map((n) => reviewKey(n, g.childJobName, g.childBuildNumber))))
    const next: Record<string, SelectedGroup> = {}
    for (const [id, g] of Object.entries(selectionRef.current)) {
      const kept = g.testNames.filter((n) => {
        const k = reviewKey(n, g.childJobName, g.childBuildNumber)
        return !coveredKeys.has(k) || failedKeys.has(k)
      })
      if (kept.length > 0) next[id] = { ...g, testNames: kept }
    }
    if (Object.keys(next).length === 0) {
      dispatch({ type: 'CLEAR_SELECTION' })
      return true
    }
    dispatch({ type: 'REPLACE_SELECTION', payload: next })
    return false
  }

  async function apply(action: PendingAction) {
    setRunning(true)
    setError(null)
    try {
      const { failed, warning } = await action.run()
      const emptied = keepSelectionAfterRun(scopes, failed)
      // Both can happen at once: some writes failed and a post-write step did not land.
      const names = failed.flatMap((g) => g.testNames)
      const failureText = names.length > 0 ? `Failed to update ${names.length} of ${scopes.length}: ${names.join(', ')}. ` : ''
      const message = `${failureText}${warning ?? ''}`.trim()
      if (emptied) setStandaloneError(message || null)
      else setError(message || null)
    } catch {
      setError('Bulk update failed. Please try again.')
    } finally {
      setRunning(false)
      setPending(null)
    }
  }

  /** One request per signature group — the backend propagates an override to every
   *  test sharing the error signature, so a per-test call would write the same
   *  rows (and history entries) once per test. */
  function overrideGroups(run: (group: SelectedGroup, rep: FailureScope) => Promise<void>) {
    return runBatched(
      overrideTargets,
      async (g) => run(g, { testName: g.testNames[0], childJobName: g.childJobName, childBuildNumber: g.childBuildNumber }),
      BULK_BATCH_SIZE,
    )
  }

  function requestReview(next: boolean) {
    setPending({
      title: next ? 'Mark as reviewed' : 'Mark as unreviewed',
      description: `Apply to ${scopeText}?`,
      confirmLabel: next ? 'Mark reviewed' : 'Unmark reviewed',
      run: async () => {
        const failed = await runBatched(scopes, async (scope) => {
          const res = await putReviewed(jobId, scope, next)
          dispatch({
            type: 'SET_REVIEW',
            payload: {
              key: reviewKey(scope.testName, scope.childJobName, scope.childBuildNumber),
              state: scopedReviewState(next, res.reviewed_by),
            },
          })
        }, BULK_BATCH_SIZE)
        notifyReviewChanged(jobId)
        return { failed: failedGroups(groups, failed) }
      },
    })
  }

  function requestClassification(classification: string) {
    setPending({
      title: `Set classification to ${classification}`,
      description: `The override applies to ${overrideScopeText}?`,
      confirmLabel: 'Apply',
      run: async () => ({
        failed: await overrideGroups(async (g, rep) => {
          await putOverrideClassification(jobId, rep, classification)
          dispatch({
            type: 'OVERRIDE_CLASSIFICATION',
            payload: { ...rep, testNames: g.testNames, classification },
          })
        }),
      }),
    })
  }

  function requestPattern(pattern: string) {
    setPending({
      title: `Set pattern to ${pattern}`,
      description: `The override applies to ${overrideScopeText}?`,
      confirmLabel: 'Apply',
      run: async () => ({
        failed: await overrideGroups(async (g, rep) => {
          await putOverridePattern(jobId, rep, pattern)
          dispatch({
            type: 'OVERRIDE_PATTERN',
            payload: { ...rep, testNames: g.testNames, pattern },
          })
        }),
      }),
    })
  }

  function requestTrackIn() {
    const url = trackUrl.trim()
    if (!url || trackedInUrlError(url)) return
    setTrackOpen(false)
    setPending({
      title: 'Link to issue',
      description: `Link ${url} to ${scopeText}?`,
      confirmLabel: 'Link',
      run: async () => {
        const failed = await runBatched(scopes, (scope) => putTrackedIn(jobId, scope, url, detectTrackerType(url)), BULK_BATCH_SIZE)
        setTrackUrl('')
        try {
          const tracked = await getTrackedIn(jobId)
          dispatch({ type: 'SET_TRACKED_IN', payload: tracked.tracked_in ?? {} })
          return { failed: failedGroups(groups, failed) }
        } catch {
          // Only the refresh failed. The writes still stand, but a link that was
          // never written must not be claimed as saved — report both.
          const notWritten = failedGroups(groups, failed)
          return notWritten.length > 0
            ? { failed: notWritten, warning: 'Refreshing the tracked links also failed.' }
            : { failed: [], warning: 'Links saved, but refreshing the tracked links failed.' }
        }
      },
    })
  }

  const allReviewed = scopes.every(
    (s) => reviews[reviewKey(s.testName, s.childJobName, s.childBuildNumber)]?.reviewed,
  )

  return (
    <>
      <div className="fixed bottom-0 left-0 right-0 z-50 border-t border-border-muted bg-surface-card/95 backdrop-blur-sm px-6 py-3 animate-slide-up">
        <div className="mx-auto flex max-w-screen-xl flex-wrap items-center gap-3">
          <span className="text-sm text-text-secondary">
            {scopes.length} {scopes.length === 1 ? 'test' : 'tests'} selected in {groups.length} {groups.length === 1 ? 'failure' : 'failures'}
          </span>
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="outline" size="sm" disabled={running} onClick={() => requestReview(!allReviewed)}>
              <CheckCircle2 className="h-3.5 w-3.5 mr-1" />
              {allReviewed ? 'Unmark reviewed' : 'Mark reviewed'}
            </Button>
            <Select value="" onValueChange={requestClassification} disabled={running}>
              <SelectTrigger aria-label="Bulk classification" className="h-8 w-40 text-xs">
                <SelectValue placeholder="Classification..." />
              </SelectTrigger>
              <SelectContent>
                {OVERRIDE_CLASSIFICATIONS.map((c) => (
                  <SelectItem key={c} value={c}>{c}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select value="" onValueChange={requestPattern} disabled={running}>
              <SelectTrigger aria-label="Bulk pattern" className="h-8 w-32 text-xs">
                <SelectValue placeholder="Pattern..." />
              </SelectTrigger>
              <SelectContent>
                {PATTERNS.map((p) => (
                  <SelectItem key={p} value={p}>{p}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button variant="outline" size="sm" disabled={running} onClick={() => { setError(null); setTrackUrl(''); setTrackOpen(true) }}>
              <Link2 className="h-3.5 w-3.5 mr-1" />
              Track in...
            </Button>
          </div>
          <div className="ml-auto flex items-center gap-3">
            {error && <span role="alert" className="text-xs text-signal-red">{error}</span>}
            <Button variant="ghost" size="sm" disabled={running} onClick={() => dispatch({ type: 'CLEAR_SELECTION' })}>
              Clear
            </Button>
          </div>
        </div>
      </div>

      <BulkTrackInDialog
        open={trackOpen}
        onOpenChange={setTrackOpen}
        scopeText={scopeText}
        url={trackUrl}
        onUrlChange={setTrackUrl}
        onSubmit={requestTrackIn}
      />

      <ConfirmDialog
        open={pending !== null}
        onOpenChange={(o) => { if (!o && !running) setPending(null) }}
        title={pending?.title ?? ''}
        description={pending?.description ?? ''}
        confirmLabel={pending?.confirmLabel ?? 'Apply'}
        onConfirm={() => { if (pending) void apply(pending) }}
        loading={running}
      />
    </>
  )
}

/** URL prompt for linking the whole selection to an existing issue. */
function BulkTrackInDialog({ open, onOpenChange, scopeText, url, onUrlChange, onSubmit }: {
  open: boolean
  onOpenChange: (open: boolean) => void
  scopeText: string
  url: string
  onUrlChange: (url: string) => void
  onSubmit: () => void
}) {
  const urlError = trackedInUrlError(url)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Link to issue</DialogTitle>
          <DialogDescription>
            The issue will be linked to every test in {scopeText}.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5">
          <label htmlFor="bulk-tracked-in-url" className="text-xs font-display uppercase tracking-widest text-text-tertiary">Issue URL</label>
          <Input
            id="bulk-tracked-in-url"
            value={url}
            onChange={(e) => onUrlChange(e.target.value)}
            placeholder="https://jira.example.com/browse/PROJ-123"
            className="text-sm"
          />
          {urlError && <p className="text-xs text-signal-red">{urlError}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={onSubmit} disabled={!url.trim() || !!urlError}>Continue</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
