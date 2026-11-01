import { useState } from 'react'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Upload, CheckCircle2, AlertTriangle, XCircle } from 'lucide-react'
import { ExporterPushButton } from '@/components/shared/ExporterPushButton'
import { useExporterPush } from './useExporterPush'
import { exporterEnabled, useReportState } from '@/pages/report/reportState'

interface GreenwaveButtonProps {
  jobId: string
  childJobName?: string
  childBuildNumber?: number
  hasFailures: boolean
}

export function GreenwaveButton({ jobId, childJobName, childBuildNumber, hasFailures }: GreenwaveButtonProps) {
  const { exporters, greenwaveWaiversEnabled } = useReportState()
  const { push, pushing, result, error } = useExporterPush({ jobId, plugin: 'greenwave', childJobName, childBuildNumber })
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [resultDialogOpen, setResultDialogOpen] = useState(false)
  const [subjectIdentifier, setSubjectIdentifier] = useState('')
  const [waiverComment, setWaiverComment] = useState('')
  const [validationError, setValidationError] = useState('')

  if (!exporterEnabled(exporters, 'greenwave')) return null

  const handlePush = async () => {
    // The server requires an explicit subject identifier when waivers are
    // enabled — block the request instead of firing one that will 422.
    if (greenwaveWaiversEnabled && !subjectIdentifier.trim()) {
      setValidationError('Subject identifier is required when waivers are enabled.')
      return
    }
    setValidationError('')
    setConfirmOpen(false)
    await push({ subject_identifier: subjectIdentifier, waiver_comment: waiverComment })
    setResultDialogOpen(true)
  }

  // Backend precedence: a hard error / failed push, then a partial push,
  // then a no-op (nothing matched the outcome map — not a failure), then success.
  const pushed = result?.pushed ?? 0
  const hasResultErrors = (result?.errors?.length ?? 0) > 0
  const isFailure = !!error || (result !== null && !result.success && (hasResultErrors || pushed > 0))
  const isPartial = !isFailure && result !== null && result.success && hasResultErrors
  const isNoop = !isFailure && !isPartial && result !== null && pushed === 0 && !error && !hasResultErrors
  const waived = result?.waived ?? 0

  return (
    <>
      <ExporterPushButton
        label="Push to Greenwave"
        pushingLabel="Pushing..."
        pushing={pushing}
        disabled={!hasFailures}
        disabledHint="No failures to push"
        onClick={() => setConfirmOpen(true)}
      />

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="sm:max-w-[480px] bg-surface-card border-border-default">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Upload className="h-5 w-5 text-text-secondary" />
              Push to Greenwave
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <label htmlFor="greenwave-subject-identifier" className="text-xs font-display uppercase tracking-widest text-text-tertiary">
                Subject identifier {greenwaveWaiversEnabled ? '(required)' : '(optional)'}
              </label>
              <Input
                id="greenwave-subject-identifier"
                value={subjectIdentifier}
                onChange={(e) => setSubjectIdentifier(e.target.value)}
                placeholder="build/artifact NVR"
                aria-invalid={!!validationError}
              />
              <p className="text-xs text-text-tertiary">
                The build/artifact NVR the results and waivers apply to. Results-only pushes fall back to the job name.
              </p>
              {validationError && (
                <p className="text-xs text-signal-red" role="alert">{validationError}</p>
              )}
            </div>
            <div className="space-y-2">
              <label htmlFor="greenwave-waiver-comment" className="text-xs font-display uppercase tracking-widest text-text-tertiary">Waiver comment (optional)</label>
              <Textarea
                id="greenwave-waiver-comment"
                value={waiverComment}
                onChange={(e) => setWaiverComment(e.target.value)}
                placeholder="Human comment included in waiver submissions"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handlePush} disabled={pushing}>
              Push
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={resultDialogOpen} onOpenChange={setResultDialogOpen}>
        <DialogContent className="sm:max-w-[520px] bg-surface-card border-border-default overflow-hidden">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {isFailure ? (
                <><XCircle className="h-5 w-5 text-signal-red" /> Failed to push to Greenwave.</>
              ) : isPartial ? (
                <><AlertTriangle className="h-5 w-5 text-signal-orange" /> Some results could not be pushed to Greenwave.</>
              ) : isNoop ? (
                <><AlertTriangle className="h-5 w-5 text-signal-orange" /> No results could be pushed — no failure matched GREENWAVE_OUTCOME_MAP.</>
              ) : (
                <><CheckCircle2 className="h-5 w-5 text-signal-green" /> Pushed {pushed} result{pushed !== 1 ? 's' : ''} to Greenwave.</>
              )}
            </DialogTitle>
          </DialogHeader>

          <div className="space-y-3 py-2 min-w-0 text-xs text-text-tertiary">
            {error ? (
              <p className="text-signal-red" role="alert">{error}</p>
            ) : result && (
              <>
                <div className="flex flex-wrap gap-x-4 gap-y-0.5">
                  <span>Pushed: {pushed}</span>
                  <span>Waived: {waived}</span>
                  <span>Skipped: {result.skipped ?? 0}</span>
                </div>
                {result.message && <p>{result.message}</p>}
                {hasResultErrors && (
                  <ul className="list-disc pl-4 space-y-1 text-signal-orange" role="alert">
                    {result.errors!.map((e, i) => (
                      <li key={i}>{e}</li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setResultDialogOpen(false)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
