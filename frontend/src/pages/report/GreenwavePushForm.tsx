import { useId, useState } from 'react'
import { Loader2, Upload } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { useExporterPush } from './useExporterPush'
import { useReportState } from '@/pages/report/reportState'
import { proposeWaiverComment } from '@/lib/greenwaveComment'
import { GreenwavePushResultView } from './GreenwavePushResultView'
import type { FailureAnalysis } from '@/types'

interface GreenwavePushFormProps {
  jobId: string
  jobName: string
  buildNumber?: number | string
  childJobName?: string
  childBuildNumber?: number
  failures: FailureAnalysis[]
  /** Called after a push roundtrip that returned a result. */
  onPushed?: () => void
}

/**
 * The one Greenwave submit surface — rendered by both GreenwaveButton's
 * dialog and the all-reviewed prompt. The push is gated client-side behind
 * an explicit approval of the exact text that will be written to
 * ResultsDB/WaiverDB, so a request that would 422 is never fired.
 */
export function GreenwavePushForm({
  jobId,
  jobName,
  buildNumber,
  childJobName,
  childBuildNumber,
  failures,
  onPushed,
}: GreenwavePushFormProps) {
  const { greenwaveWaiversEnabled, classifications } = useReportState()
  const { push, pushing, result, error } = useExporterPush({ jobId, plugin: 'greenwave', childJobName, childBuildNumber })
  const [subjectIdentifier, setSubjectIdentifier] = useState('')
  const [comment, setComment] = useState(() =>
    proposeWaiverComment({ jobName, buildNumber, failures, childJobName, childBuildNumber, classifications }),
  )
  const [approved, setApproved] = useState(false)
  const uid = useId()

  // Re-seed the proposal when the job/build changes (render-time reset per
  // React docs — the parent is keyed by jobId, this guards unkeyed reuse).
  const seedKey = `${jobName}#${buildNumber ?? ''}`
  const [prevSeedKey, setPrevSeedKey] = useState(seedKey)
  if (prevSeedKey !== seedKey) {
    setPrevSeedKey(seedKey)
    setComment(
      proposeWaiverComment({ jobName, buildNumber, failures, childJobName, childBuildNumber, classifications }),
    )
    setApproved(false)
  }

  const missingIdentifier = greenwaveWaiversEnabled && !subjectIdentifier.trim()
  const canPush = approved && !missingIdentifier
  const disabledReason = !approved
    ? 'Approve the text above to push.'
    : missingIdentifier
      ? 'Subject identifier is required when waivers are enabled.'
      : null

  const handlePush = async () => {
    // Client-side gate — never fire a request that would 422.
    if (!canPush || pushing) return
    const res = await push({ subject_identifier: subjectIdentifier, waiver_comment: comment })
    if (res && onPushed) onPushed()
  }

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <label htmlFor={`${uid}-subject`} className="text-xs font-display uppercase tracking-widest text-text-tertiary">
          Subject identifier {greenwaveWaiversEnabled ? '(required)' : '(optional)'}
        </label>
        <Input
          id={`${uid}-subject`}
          value={subjectIdentifier}
          onChange={(e) => setSubjectIdentifier(e.target.value)}
          placeholder="build/artifact NVR"
        />
        <p className="text-xs text-text-tertiary">
          The build/artifact NVR the results and waivers apply to. Results-only pushes fall back to the job name.
        </p>
      </div>

      <div className="space-y-2">
        <label htmlFor={`${uid}-comment`} className="text-xs font-display uppercase tracking-widest text-text-tertiary">
          Waiver comment
        </label>
        <Textarea
          id={`${uid}-comment`}
          value={comment}
          onChange={(e) => {
            setComment(e.target.value)
            // The approval covers the exact text — editing voids it.
            setApproved(false)
          }}
        />
      </div>

      <div className="space-y-1.5 rounded-md border border-border-default bg-surface-elevated p-3 font-mono text-xs text-text-secondary">
        <p>Subject identifier: {subjectIdentifier.trim() || '(job name — results only)'}</p>
        <p>Waivers: {greenwaveWaiversEnabled && subjectIdentifier.trim() ? 'on' : 'off'}</p>
        <p>Results: {failures.length} failure(s)</p>
        <p>Text: {comment.replace(/\s+/g, ' ').trim()}</p>
      </div>

      <label htmlFor={`${uid}-approve`} className="flex items-start gap-2 text-xs text-text-secondary">
        <Checkbox
          id={`${uid}-approve`}
          checked={approved}
          onCheckedChange={(v) => setApproved(v === true)}
          className="mt-0.5"
        />
        <span>
          I approve the text above. It will be written to ResultsDB and WaiverDB as part of this push.
        </span>
      </label>

      <div className="space-y-1.5">
        <Button onClick={handlePush} disabled={!canPush || pushing}>
          {pushing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
          {pushing ? 'Pushing...' : 'Push to Greenwave'}
        </Button>
        {disabledReason && <p className="text-xs text-text-tertiary">{disabledReason}</p>}
      </div>

      <GreenwavePushResultView result={result} error={error} />
    </div>
  )
}
