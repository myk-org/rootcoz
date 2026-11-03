import { useId, useMemo, useState } from 'react'
import { Loader2, Upload } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Checkbox } from '@/components/ui/checkbox'
import { useExporterPush } from './useExporterPush'
import { useReportState } from '@/pages/report/reportState'
import { proposeWaiverComment } from '@/lib/greenwaveComment'
import { GreenwavePushResultView } from './GreenwavePushResultView'
import type { ExporterPushResult, FailureAnalysis } from '@/types'

interface GreenwavePushFormProps {
  jobId: string
  jobName: string
  buildNumber?: number | string
  childJobName?: string
  childBuildNumber?: number
  failures: FailureAnalysis[]
  /** Called after a push roundtrip; receives the result (null when the request failed). */
  onPushed?: (result: ExporterPushResult | null) => void
}

/**
 * The one Greenwave submit surface — rendered by both GreenwaveButton's
 * dialog and the all-reviewed prompt. The push is gated client-side behind
 * an explicit approval of the comment text sent with the push: the server
 * composes the final WaiverDB comment around it, and ResultsDB stores only
 * the outcome per failure. A request that would 422 is never fired.
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
  // The proposal is derived, not seeded once: the effective classifications
  // arrive after mount (and a reviewer can change one while the dialog is
  // open), and a summary showing stale counts would describe different labels
  // than the ones the server uses for the results and waivers it files.
  const proposal = useMemo(
    () =>
      proposeWaiverComment({
        jobName,
        buildNumber,
        failures,
        childJobName,
        childBuildNumber,
        classifications,
      }),
    [jobName, buildNumber, failures, childJobName, childBuildNumber, classifications],
  )
  const [comment, setComment] = useState(proposal)
  // Once the operator edits the text we stop overwriting it — but a new
  // proposal still voids the approval, because it no longer matches the text
  // that was approved.
  const [edited, setEdited] = useState(false)
  const [approved, setApproved] = useState(false)
  const uid = useId()

  const [prevProposal, setPrevProposal] = useState(proposal)
  if (prevProposal !== proposal) {
    setPrevProposal(proposal)
    if (!edited) setComment(proposal)
    setApproved(false)
  }

  // One computation for every waiver claim: waivers are only written when
  // enabled AND a subject identifier is present.
  const hasIdentifier = !!subjectIdentifier.trim()
  const waiversWritten = greenwaveWaiversEnabled && hasIdentifier
  // Every Greenwave write is a gating write: a ResultsDB result carries
  // subject_type (koji_build by default), so the identifier is required even
  // when no waiver is filed. The server refuses a push without it (422).
  const missingIdentifier = !hasIdentifier
  const canPush = approved && !missingIdentifier
  const disabledReason = !approved
    ? 'Approve the text above to push.'
    : missingIdentifier
      ? 'Subject identifier is required: ResultsDB and WaiverDB are both keyed on it.'
      : null

  const handlePush = async () => {
    // Client-side gate — never fire a request that would 422.
    if (!canPush || pushing) return
    const res = await push({ subject_identifier: subjectIdentifier, waiver_comment: comment })
    // Whatever the outcome, the approval is spent: re-pushing the same
    // payload would create a second ResultsDB group and duplicate waivers.
    setApproved(false)
    onPushed?.(res)
  }

  // The approval covers the exact payload — any edit voids it.
  const onPayloadEdit = () => setApproved(false)

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <label htmlFor={`${uid}-subject`} className="text-xs font-display uppercase tracking-widest text-text-tertiary">
          Subject identifier (required)
        </label>
        <Input
          id={`${uid}-subject`}
          value={subjectIdentifier}
          onChange={(e) => {
            setSubjectIdentifier(e.target.value)
            onPayloadEdit()
          }}
          placeholder="build/artifact NVR"
        />
        <p className="text-xs text-text-tertiary">
          The build/artifact NVR the results and waivers apply to. There is no job-name fallback — a CI job name is never used as a gating subject.
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
            setEdited(true)
            setComment(e.target.value)
            onPayloadEdit()
          }}
        />
      </div>

      <div className="space-y-1.5 rounded-md border border-border-default bg-surface-elevated p-3 font-mono text-xs text-text-secondary">
        <p>Subject identifier: {subjectIdentifier.trim() || '(required)'}</p>
        <p>Waivers: {waiversWritten ? 'on' : 'off'}</p>
        <p>Results: {failures.length} failure(s)</p>
        <p>ResultsDB: outcome per failure (PASSED / FAILED / INFO / NEEDS_INSPECTION)</p>
        {waiversWritten && <p>WaiverDB: waiver for waivable failures</p>}
        <p>Text: {comment.replace(/\s+/g, ' ').trim()}</p>
      </div>
      {waiversWritten && (
        <p className="text-xs text-text-tertiary">
          The text above is included in the WaiverDB comment, which the server completes with
          the pusher's name, the classification, the analysis details and the reviewer.
          ResultsDB stores the outcome only.
        </p>
      )}

      <label htmlFor={`${uid}-approve`} className="flex items-start gap-2 text-xs text-text-secondary">
        <Checkbox
          id={`${uid}-approve`}
          checked={approved}
          onCheckedChange={(v) => setApproved(v === true)}
          className="mt-0.5"
        />
        <span>
          I approve the text above
          {waiversWritten ? '. It will be submitted with the waiver.' : '.'}
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
