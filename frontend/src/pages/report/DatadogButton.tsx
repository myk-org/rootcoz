import { useState, useCallback } from 'react'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import { api } from '@/lib/api'
import { pushScopeQuery } from '@/lib/pushScopes'
import {
  isUnreviewedPushRejection,
  pushRejectionDetail,
  unreviewedPushRejectionMessage,
  unreviewedPushTooltip,
} from '@/lib/pushGate'
import { useUnreviewedPushGate } from './pushGate'
import { Upload, Loader2, CheckCircle2, AlertTriangle, XCircle } from 'lucide-react'
import type { DatadogPushResult } from '@/types'

interface DatadogPushMetadataProps {
  jobName?: string
  buildNumber?: number | string
  className?: string
}

function DatadogPushMetadata({ jobName, buildNumber, className }: DatadogPushMetadataProps) {
  const buildLabel = buildNumber != null && buildNumber !== '' ? String(buildNumber) : null
  return (
    <dl className={className}>
      {jobName && (
        <>
          <dt className="font-medium">Job</dt>
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <dd className="font-mono truncate">{jobName}</dd>
              </TooltipTrigger>
              <TooltipContent className="max-w-md break-all">{jobName}</TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </>
      )}
      {buildLabel && (
        <>
          <dt className="font-medium">Build</dt>
          <dd className="font-mono">#{buildLabel}</dd>
        </>
      )}
    </dl>
  )
}

interface DatadogButtonProps {
  jobId: string
  jobName: string
  buildNumber?: number | string
  childJobName?: string
  childBuildNumber?: number
  hasFailures: boolean
}

export function DatadogButton({ jobId, jobName, buildNumber, childJobName, childBuildNumber, hasFailures }: DatadogButtonProps) {
  const displayJobName = childJobName ?? jobName
  const displayBuildNumber = childBuildNumber ?? buildNumber
  const { unreviewedCount, allowUnreviewedPush } = useUnreviewedPushGate('datadog', {
    childJobName,
    childBuildNumber,
  })
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [pushing, setPushing] = useState(false)
  const [resultDialogOpen, setResultDialogOpen] = useState(false)
  const [pushResult, setPushResult] = useState<DatadogPushResult | null>(null)
  const [pushFailed, setPushFailed] = useState(false)
  // Actionable 409 message from the unreviewed-failure push gate
  const [gateMessage, setGateMessage] = useState<string | null>(null)
  // Rejection detail (403, invalid key, …) for generic push failures
  const [pushErrorDetail, setPushErrorDetail] = useState<string | null>(null)
  // The gate blocks the push up front (server would 409 anyway): the job has
  // unreviewed failures and DD_ALLOW_UNREVIEWED_PUSH is off.
  const gateBlocked = unreviewedCount > 0 && !allowUnreviewedPush

  const handlePush = useCallback(async () => {
    setConfirmOpen(false)
    setPushing(true)
    setPushFailed(false)
    setGateMessage(null)
    setPushErrorDetail(null)
    setPushResult(null)
    try {
      const qs = pushScopeQuery({ childJobName, childBuildNumber })
      const result = await api.post<DatadogPushResult>(`/results/${jobId}/push/datadog${qs}`)
      setPushResult(result)
      setResultDialogOpen(true)
    } catch (err) {
      if (isUnreviewedPushRejection(err)) {
        setGateMessage(unreviewedPushRejectionMessage(err, 'datadog'))
      } else {
        setPushFailed(true)
        setPushErrorDetail(pushRejectionDetail(err))
      }
      setResultDialogOpen(true)
    } finally {
      setPushing(false)
    }
  }, [jobId, childJobName, childBuildNumber])

  const handleClose = useCallback(() => {
    setResultDialogOpen(false)
  }, [])

  const hasResultErrors = !!(pushResult && pushResult.errors.length > 0)
  const hasSkipped = !!(pushResult && pushResult.skipped > 0)
  // `success=false` with nothing pushed is a failure even without per-event
  // errors (e.g. the exporter aborted: API-key rejection, no failures) —
  // it must never render as a green "Pushed 0 events".
  const isFullFailure = pushFailed || !!(pushResult && pushResult.pushed === 0 && (!pushResult.success || hasResultErrors))
  const isPartialSuccess = !!pushResult && pushResult.pushed > 0 && (!pushResult.success || hasResultErrors || hasSkipped)

  return (
    <>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <span>
              <Button
                variant="ghost"
                size="sm"
                className="gap-1.5 text-xs"
                onClick={() => setConfirmOpen(true)}
                disabled={pushing || !hasFailures || gateBlocked}
              >
                {pushing ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Upload className="h-3.5 w-3.5" />
                )}
                {pushing ? 'Pushing...' : 'Push to Datadog'}
              </Button>
            </span>
          </TooltipTrigger>
          {!hasFailures ? (
            <TooltipContent>No failures to push</TooltipContent>
          ) : gateBlocked ? (
            <TooltipContent data-testid="datadog-push-gate-tooltip">
              {unreviewedPushTooltip(unreviewedCount, 'datadog')}
            </TooltipContent>
          ) : null}
        </Tooltip>
      </TooltipProvider>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="sm:max-w-[400px] bg-surface-card border-border-default">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Upload className="h-5 w-5 text-text-secondary" />
              Confirm Push
            </DialogTitle>

          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handlePush}>
              Push
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={resultDialogOpen} onOpenChange={setResultDialogOpen}>
        <DialogContent className="sm:max-w-[520px] bg-surface-card border-border-default overflow-hidden">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {gateMessage ? (
                <><AlertTriangle className="h-5 w-5 text-signal-orange" /> Push blocked — unreviewed failures.</>
              ) : isFullFailure ? (
                <><XCircle className="h-5 w-5 text-signal-red" /> Failed to push events to Datadog.</>
              ) : isPartialSuccess ? (
                <><AlertTriangle className="h-5 w-5 text-signal-orange" /> Some events could not be pushed.</>
              ) : (
                <><CheckCircle2 className="h-5 w-5 text-signal-green" /> Pushed {pushResult?.pushed ?? 0} event{pushResult?.pushed !== 1 ? 's' : ''} to Datadog.</>
              )}
            </DialogTitle>
          </DialogHeader>

          {(gateMessage || isFullFailure || isPartialSuccess) && (
            <div className="space-y-3 py-2 min-w-0">
              <DatadogPushMetadata
                jobName={displayJobName}
                buildNumber={displayBuildNumber}
                className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs text-text-tertiary"
              />
              {gateMessage && (
                <p className="text-xs text-text-secondary" data-testid="datadog-push-gate-message">{gateMessage}</p>
              )}
              {pushErrorDetail && (
                <p className="text-xs text-text-secondary" data-testid="datadog-push-message">{pushErrorDetail}</p>
              )}
              {pushResult?.message && (
                <p className="text-xs text-text-secondary" data-testid="datadog-push-message">{pushResult.message}</p>
              )}
              {pushResult && pushResult.errors.length > 0 && (
                <ul className="space-y-1 text-xs text-text-tertiary" data-testid="datadog-push-errors">
                  {pushResult.errors.map((err, i) => (
                    <li key={i} className="font-mono break-all">{err}</li>
                  ))}
                </ul>
              )}
              {pushResult && pushResult.skipped > 0 && (
                <p className="text-xs text-text-tertiary">{pushResult.skipped} event{pushResult.skipped !== 1 ? 's' : ''} skipped</p>
              )}
            </div>
          )}

          <DialogFooter>
            <Button variant="outline" onClick={handleClose}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
