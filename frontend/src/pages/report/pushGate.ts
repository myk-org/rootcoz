import { useMemo } from 'react'
import { countUnreviewedFailures } from '@/lib/failureKeys'
import { useReportState } from '@/pages/report/reportState'
import type { GatedExporter } from '@/lib/pushGate'

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
