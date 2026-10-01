import { useState } from 'react'
import { useReportState, useReportDispatch, reviewKey } from './ReportContext'
import {
  type FailureScope,
  getTrackedIn,
  notifyReviewChanged,
  putOverrideClassification,
  putOverridePattern,
  putReviewed,
  putTrackedIn,
  runBatched,
  scopedReviewState,
  selectedScopes,
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
  /** Runs the bulk mutation; resolves with the scopes that failed. */
  run: () => Promise<FailureScope[]>
}

/** Bulk-apply the existing per-failure edits to the selected failure groups. */
export function BulkUpdateBar() {
  const { result, selection, reviews } = useReportState()
  const dispatch = useReportDispatch()
  const [pending, setPending] = useState<PendingAction | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [trackOpen, setTrackOpen] = useState(false)
  const [trackUrl, setTrackUrl] = useState('')

  const groups = Object.values(selection)
  if (groups.length === 0) return null
  const jobId = result?.job_id ?? ''
  const scopes = selectedScopes(groups)
  const scopeText = `${scopes.length} ${scopes.length === 1 ? 'test' : 'tests'} in ${groups.length} ${groups.length === 1 ? 'failure' : 'failures'}`

  async function apply(action: PendingAction) {
    setRunning(true)
    setError(null)
    try {
      const failed = await action.run()
      if (failed.length === 0) {
        dispatch({ type: 'CLEAR_SELECTION' })
      } else {
        setError(`Failed to update ${failed.length} of ${scopes.length}: ${failed.map((s) => s.testName).join(', ')}`)
      }
    } catch {
      setError('Bulk update failed. Please try again.')
    } finally {
      setRunning(false)
      setPending(null)
    }
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
        return failed
      },
    })
  }

  function requestClassification(classification: string) {
    setPending({
      title: `Set classification to ${classification}`,
      description: `Apply to ${scopeText}?`,
      confirmLabel: 'Apply',
      run: async () => {
        const failed = await runBatched(scopes, async (scope) => {
          await putOverrideClassification(jobId, scope, classification)
          dispatch({
            type: 'OVERRIDE_CLASSIFICATION',
            payload: { testName: scope.testName, classification, childJobName: scope.childJobName, childBuildNumber: scope.childBuildNumber },
          })
        }, BULK_BATCH_SIZE)
        return failed
      },
    })
  }

  function requestPattern(pattern: string) {
    setPending({
      title: `Set pattern to ${pattern}`,
      description: `Apply to ${scopeText}?`,
      confirmLabel: 'Apply',
      run: async () => {
        const failed = await runBatched(scopes, async (scope) => {
          await putOverridePattern(jobId, scope, pattern)
          dispatch({
            type: 'OVERRIDE_PATTERN',
            payload: { testName: scope.testName, pattern, childJobName: scope.childJobName, childBuildNumber: scope.childBuildNumber },
          })
        }, BULK_BATCH_SIZE)
        return failed
      },
    })
  }

  function requestTrackIn() {
    const url = trackUrl.trim()
    if (!url) return
    setTrackOpen(false)
    setPending({
      title: 'Link to issue',
      description: `Link ${url} to ${scopeText}?`,
      confirmLabel: 'Link',
      run: async () => {
        const failed = await runBatched(scopes, (scope) => putTrackedIn(jobId, scope, url, detectTrackerType(url)), BULK_BATCH_SIZE)
        const tracked = await getTrackedIn(jobId)
        dispatch({ type: 'SET_TRACKED_IN', payload: tracked.tracked_in ?? {} })
        setTrackUrl('')
        return failed
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
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={onSubmit} disabled={!url.trim()}>Continue</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
