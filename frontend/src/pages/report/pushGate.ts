import { useMemo } from 'react'
import { ApiError, extractApiDetail } from '@/lib/api'
import { countUnreviewedFailures } from '@/lib/failureKeys'
import { useReportState } from '@/pages/report/reportState'

/** Exporters whose manual pushes are gated on reviewed failures. */
export type GatedExporter = 'reportportal' | 'datadog'

/** Server Setting escape hatch per exporter, as named in user-facing text. */
export const PUSH_GATE_SETTINGS: Record<GatedExporter, string> = {
  reportportal: 'RP_ALLOW_UNREVIEWED_PUSH',
  datadog: 'DD_ALLOW_UNREVIEWED_PUSH',
}

/** Tooltip shown while a push button is disabled by the review gate. */
export function unreviewedPushTooltip(count: number, exporter: GatedExporter): string {
  return `${count} failure(s) not yet reviewed — review them before pushing (or ask an admin to enable ${PUSH_GATE_SETTINGS[exporter]})`
}

/** The report state a push button needs for the unreviewed-failure gate:
 *  how many failures still lack a review, and whether this exporter's
 *  escape hatch is on (hatch on → the gate never blocks). */
export function useUnreviewedPushGate(exporter: GatedExporter): {
  unreviewedCount: number
  allowUnreviewedPush: boolean
} {
  const { result, reviews, rpAllowUnreviewedPush, ddAllowUnreviewedPush } = useReportState()
  const unreviewedCount = useMemo(
    () =>
      result
        ? countUnreviewedFailures(result.failures ?? [], result.child_job_analyses ?? [], reviews)
        : 0,
    [result, reviews],
  )
  const allowUnreviewedPush = exporter === 'reportportal' ? rpAllowUnreviewedPush : ddAllowUnreviewedPush
  return { unreviewedCount, allowUnreviewedPush }
}

/** True when the server rejected a push with the unreviewed-failure gate (409). */
export function isUnreviewedPushRejection(err: unknown): boolean {
  return (
    err instanceof ApiError &&
    err.status === 409 &&
    !!extractApiDetail(err)?.includes('unreviewed failure(s)')
  )
}

/** Actionable message for a gate rejection — deliberately distinct from the
 *  generic red push-failure banner: points at the unreviewed failures on the
 *  page and names the setting an admin can flip to unblock. */
export function unreviewedPushRejectionMessage(err: unknown, exporter: GatedExporter): string {
  const detail = extractApiDetail(err) ?? 'job has unreviewed failure(s)'
  return `${detail} Review the unreviewed failures on this page and push again, or ask an admin to enable ${PUSH_GATE_SETTINGS[exporter]}.`
}

/** User-facing reason a push request was rejected, so operators see WHY
 *  (e.g. 403, invalid key) instead of a generic "push request failed". */
export function pushRejectionDetail(err: unknown): string {
  const detail = extractApiDetail(err)
  if (detail) return detail
  if (err instanceof Error && err.message) return err.message
  return 'push request failed'
}
