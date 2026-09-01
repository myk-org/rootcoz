import { useState, useCallback } from 'react'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import {
  ExporterPushControls,
  type ExporterPushStatus,
} from '@/components/shared/ExporterPushControls'
import { useExporterPush } from '@/lib/useExporterPush'
import type { GreenwavePushResult } from '@/types'

interface GreenwaveButtonProps {
  jobId: string
  childJobName?: string
  childBuildNumber?: number
  hasFailures: boolean
}

export function GreenwaveButton({ jobId, childJobName, childBuildNumber, hasFailures }: GreenwaveButtonProps) {
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
  } = useExporterPush<GreenwavePushResult>()

  const [subjectIdentifier, setSubjectIdentifier] = useState('')
  const [waiverComment, setWaiverComment] = useState('')

  const handlePush = useCallback(async () => {
    const normalizedSubject = subjectIdentifier.trim()
    const normalizedComment = waiverComment.trim()
    const body = {
      ...(normalizedSubject ? { subject_identifier: normalizedSubject } : {}),
      ...(normalizedComment ? { waiver_comment: normalizedComment } : {}),
    }
    await runPush(`/results/${jobId}/push/greenwave`, {
      body,
      childJobName,
      childBuildNumber,
    })
  }, [jobId, childJobName, childBuildNumber, subjectIdentifier, waiverComment, runPush])

  const hasResultErrors = !!(pushResult && pushResult.errors.length > 0)
  const isNoop = !!(
    pushResult
    && pushResult.pushed === 0
    && pushResult.skipped > 0
    && !hasResultErrors
  )
  const resultStatus: ExporterPushStatus = pushFailed
    || (!!pushResult && !isNoop && pushResult.pushed === 0)
    ? 'failure'
    : isNoop
      ? 'noop'
      : hasResultErrors
        ? 'partial'
        : 'success'
  const resultTitle = resultStatus === 'failure'
    ? 'Failed to push to Greenwave.'
    : resultStatus === 'noop'
      ? 'No Greenwave results needed pushing.'
      : resultStatus === 'partial'
        ? 'Pushed to Greenwave with errors.'
        : 'Pushed to Greenwave successfully.'

  return (
    <ExporterPushControls
      exporterName="Greenwave"
      hasFailures={hasFailures}
      pushing={pushing}
      onRequestPush={() => {
        setSubjectIdentifier('')
        setWaiverComment('')
        setConfirmOpen(true)
      }}
      confirmOpen={confirmOpen}
      onConfirmOpenChange={setConfirmOpen}
      confirmTitle="Push to Greenwave"
      confirmClassName="sm:max-w-[500px]"
      onConfirm={handlePush}
      confirmContent={(
        <div className="space-y-4 py-2">
          <p className="text-sm text-text-secondary">
            This pushes current effective classifications to ResultsDB. If waivers are enabled server-side, each qualifying reviewed failure is also submitted to WaiverDB.
          </p>
          <div className="space-y-2">
            <label className="text-sm font-medium">Subject identifier (build NVR)</label>
            <Input
              placeholder="e.g. myproduct-bundle-registry-container-vX.Y.Z-BN"
              value={subjectIdentifier}
              onChange={(event) => setSubjectIdentifier(event.target.value)}
            />
          </div>
          <div className="space-y-2">
            <label className="text-sm font-medium">Waiver justification (optional)</label>
            <Textarea
              placeholder="Why is this being waived? (max 500 chars)"
              maxLength={500}
              value={waiverComment}
              onChange={(event) => setWaiverComment(event.target.value)}
              className="min-h-[100px]"
            />
            <div className="flex justify-end text-xs text-text-tertiary">
              {waiverComment.length}/500
            </div>
          </div>
        </div>
      )}
      resultOpen={resultDialogOpen}
      onResultOpenChange={setResultDialogOpen}
      resultStatus={resultStatus}
      resultTitle={resultTitle}
      resultContent={pushFailed ? (
        <p className="py-2 text-sm text-signal-red">{pushErrorMessage}</p>
      ) : pushResult ? (
        <div className="space-y-3 py-2 min-w-0">
          <div className="text-sm text-text-secondary">{pushResult.message}</div>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs text-text-tertiary">
            <dt className="font-medium">Pushed</dt>
            <dd className="font-mono">{pushResult.pushed}</dd>
            <dt className="font-medium">Waived</dt>
            <dd className="font-mono">{pushResult.waived}</dd>
            <dt className="font-medium">Skipped</dt>
            <dd className="font-mono">{pushResult.skipped}</dd>
            <dt className="font-medium">Errors</dt>
            <dd className="font-mono">{pushResult.errors.length}</dd>
          </dl>
          {hasResultErrors && (
            <div className="mt-2 text-xs text-signal-red max-h-32 overflow-y-auto bg-black/10 p-2 rounded">
              <ul className="list-disc pl-4 space-y-1">
                {pushResult.errors.map((error, index) => (
                  <li key={`${index}-${error}`}>{error}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      ) : null}
    />
  )
}
