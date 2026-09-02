import { useCallback } from 'react'
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import {
  ExporterPushControls,
  type ExporterPushStatus,
} from '@/components/shared/ExporterPushControls'
import { useExporterPush } from '@/lib/useExporterPush'
import type { ReportPortalPushResult } from '@/types'
import { useReportState } from './ReportContext'

interface RPPushMetadataProps {
  project?: string
  jobName?: string
  buildNumber?: number | string
  launchId?: number
  className?: string
}

function RPPushMetadata({ project, jobName, buildNumber, launchId, className }: RPPushMetadataProps) {
  const buildLabel = buildNumber != null && buildNumber !== '' ? String(buildNumber) : null
  return (
    <dl className={className}>
      {project && (
        <>
          <dt className="font-medium">Project</dt>
          <Tooltip>
            <TooltipTrigger asChild>
              <dd className="font-mono truncate">{project}</dd>
            </TooltipTrigger>
            <TooltipContent className="max-w-md break-all">{project}</TooltipContent>
          </Tooltip>
        </>
      )}
      {jobName && (
        <>
          <dt className="font-medium">Job</dt>
          <Tooltip>
            <TooltipTrigger asChild>
              <dd className="font-mono truncate">{jobName}</dd>
            </TooltipTrigger>
            <TooltipContent className="max-w-md break-all">{jobName}</TooltipContent>
          </Tooltip>
        </>
      )}
      {buildLabel && (
        <>
          <dt className="font-medium">Build</dt>
          <dd className="font-mono">#{buildLabel}</dd>
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
  buildNumber?: number | string
  childJobName?: string
  childBuildNumber?: number
  hasFailures: boolean
}

export function ReportPortalButton({ jobId, jobName, buildNumber, childJobName, childBuildNumber, hasFailures }: ReportPortalButtonProps) {
  const { reportportalProject } = useReportState()
  const displayJobName = childJobName ?? jobName
  const displayBuildNumber = childBuildNumber ?? buildNumber
  const {
    confirmOpen,
    setConfirmOpen,
    pushing,
    resultDialogOpen,
    setResultDialogOpen,
    pushResult,
    pushFailed,
    pushErrorMessage,
    runPush
  } = useExporterPush<ReportPortalPushResult>()

  const handlePush = useCallback(async () => {
    await runPush(`/results/${jobId}/push-reportportal`, {
      childJobName,
      childBuildNumber,
    })
  }, [jobId, childJobName, childBuildNumber, runPush])

  const hasResultErrors = !!(pushResult && pushResult.errors.length > 0)
  const hasUnmatched = !!(pushResult && pushResult.unmatched.length > 0)
  const isFullFailure = pushFailed || !!(pushResult && pushResult.pushed === 0 && hasResultErrors)
  const isPartialSuccess = !!(pushResult && pushResult.pushed > 0 && (hasResultErrors || hasUnmatched))
  const isNoop = !!(pushResult && pushResult.pushed === 0 && !hasResultErrors && hasUnmatched)
  const resultStatus: ExporterPushStatus = isFullFailure
    ? 'failure'
    : isPartialSuccess || isNoop
      ? 'partial'
      : 'success'
  const resultTitle = isFullFailure
    ? 'Failed to push classifications to Report Portal.'
    : isNoop
      ? 'No classifications could be matched.'
      : isPartialSuccess
        ? 'Some classifications could not be pushed.'
        : `Pushed ${pushResult?.pushed ?? 0} classification${pushResult?.pushed !== 1 ? 's' : ''} to Report Portal.`

  return (
    <ExporterPushControls
      exporterName="Report Portal"
      hasFailures={hasFailures}
      pushing={pushing}
      onRequestPush={() => setConfirmOpen(true)}
      confirmOpen={confirmOpen}
      onConfirmOpenChange={setConfirmOpen}
      confirmTitle="Confirm Push"
      onConfirm={handlePush}
      resultOpen={resultDialogOpen}
      onResultOpenChange={setResultDialogOpen}
      resultStatus={resultStatus}
      resultTitle={resultTitle}
      resultContent={pushFailed ? (
        <p className="py-2 text-sm text-signal-red">{pushErrorMessage}</p>
      ) : isFullFailure || isPartialSuccess || isNoop ? (
        <div className="space-y-3 py-2 min-w-0">
          <RPPushMetadata
            project={reportportalProject}
            jobName={displayJobName}
            buildNumber={displayBuildNumber}
            launchId={pushResult?.launch_id ?? undefined}
            className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs text-text-tertiary"
          />
        </div>
      ) : null}
    />
  )
}
