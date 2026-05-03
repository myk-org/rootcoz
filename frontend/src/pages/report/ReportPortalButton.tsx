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
import { Upload, Loader2, CheckCircle2, AlertTriangle, XCircle } from 'lucide-react'
import type { ReportPortalPushResult } from '@/types'
import { useReportState } from './ReportContext'

interface RPPushMetadataProps {
  project?: string
  jobName?: string
  buildNumber?: number
  launchId?: number
  className?: string
}

function RPPushMetadata({ project, jobName, buildNumber, launchId, className }: RPPushMetadataProps) {
  return (
    <dl className={className}>
      {project && (
        <>
          <dt className="font-medium">Project</dt>
          <dd className="font-mono truncate" title={project}>{project}</dd>
        </>
      )}
      {jobName && (
        <>
          <dt className="font-medium">Job</dt>
          <dd className="font-mono truncate" title={jobName}>{jobName}</dd>
        </>
      )}
      {buildNumber != null && (
        <>
          <dt className="font-medium">Build</dt>
          <dd className="font-mono">#{buildNumber}</dd>
        </>
      )}
      {launchId != null && (
        <>
          <dt className="font-medium">Launch ID</dt>
          <dd className="font-mono">{launchId}</dd>
        </>
      )}
    </dl>
  )
}

interface ReportPortalButtonProps {
  jobId: string
  jobName: string
  buildNumber: number
  childJobName?: string
  childBuildNumber?: number
  hasFailures: boolean
}

export function ReportPortalButton({ jobId, jobName, buildNumber, childJobName, childBuildNumber, hasFailures }: ReportPortalButtonProps) {
  const { reportportalProject } = useReportState()
  const displayJobName = childJobName ?? jobName
  const displayBuildNumber = childBuildNumber ?? buildNumber
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [pushing, setPushing] = useState(false)
  const [resultDialogOpen, setResultDialogOpen] = useState(false)
  const [pushResult, setPushResult] = useState<ReportPortalPushResult | null>(null)
  const [pushFailed, setPushFailed] = useState(false)

  const handlePush = useCallback(async () => {
    setConfirmOpen(false)
    setPushing(true)
    setPushFailed(false)
    setPushResult(null)
    try {
      const params = new URLSearchParams()
      if (childJobName) params.set('child_job_name', childJobName)
      if (childBuildNumber != null) params.set('child_build_number', String(childBuildNumber))
      const qs = params.toString()
      const result = await api.post<ReportPortalPushResult>(`/results/${jobId}/push-reportportal${qs ? `?${qs}` : ''}`)
      setPushResult(result)
      setResultDialogOpen(true)
    } catch {
      setPushFailed(true)
      setResultDialogOpen(true)
    } finally {
      setPushing(false)
    }
  }, [jobId, childJobName, childBuildNumber])

  const handleClose = useCallback(() => {
    setResultDialogOpen(false)
  }, [])

  const hasResultErrors = !!(pushResult && pushResult.errors.length > 0)
  const hasUnmatched = !!(pushResult && pushResult.unmatched.length > 0)
  const isFullFailure = pushFailed || (pushResult && pushResult.pushed === 0 && hasResultErrors)
  const isPartialSuccess = pushResult && pushResult.pushed > 0 && (hasResultErrors || hasUnmatched)
  const isNoop = !!(pushResult && pushResult.pushed === 0 && !hasResultErrors && hasUnmatched)

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
                disabled={pushing || !hasFailures}
              >
                {pushing ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Upload className="h-3.5 w-3.5" />
                )}
                {pushing ? 'Pushing...' : 'Push to Report Portal'}
              </Button>
            </span>
          </TooltipTrigger>
          {!hasFailures && (
            <TooltipContent>No failures to push</TooltipContent>
          )}
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
              {isFullFailure ? (
                <><XCircle className="h-5 w-5 text-signal-red" /> Failed to push classifications to Report Portal.</>
              ) : isPartialSuccess || isNoop ? (
                <><AlertTriangle className="h-5 w-5 text-signal-orange" /> {isNoop ? 'No classifications could be matched.' : 'Some classifications could not be pushed.'}</>
              ) : (
                <><CheckCircle2 className="h-5 w-5 text-signal-green" /> Pushed {pushResult?.pushed ?? 0} classification{pushResult?.pushed !== 1 ? 's' : ''} to Report Portal.</>
              )}
            </DialogTitle>
          </DialogHeader>

          {(isFullFailure || isPartialSuccess || isNoop) && (
            <div className="space-y-3 py-2 min-w-0">
              <RPPushMetadata
                project={reportportalProject}
                jobName={displayJobName}
                buildNumber={displayBuildNumber}
                launchId={pushResult?.launch_id ?? undefined}
                className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs text-text-tertiary"
              />


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
