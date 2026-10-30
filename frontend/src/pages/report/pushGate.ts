import { useMemo } from 'react'
import { countUnreviewedFailures } from '@/lib/failureKeys'
import { useReportState } from '@/pages/report/reportState'
import type { GatedExporter } from '@/lib/pushGate'

/** The report state a push button needs for the unreviewed-failure gate:
 *  how many failures still lack a review, and whether this exporter's
 *  escape hatch is on (hatch on → the gate never blocks).
 *
 *  A child-scoped button passes its scope: the server gates a push against
 *  exactly the subtree it exports, so a fully reviewed child must not be
 *  blocked because the parent is still unreviewed. */
export function useUnreviewedPushGate(
  exporter: GatedExporter,
  scope?: { childJobName?: string; childBuildNumber?: number },
): {
  unreviewedCount: number
  allowUnreviewedPush: boolean
} {
  const { result, reviews, rpAllowUnreviewedPush, ddAllowUnreviewedPush } = useReportState()
  const childJobName = scope?.childJobName
  const childBuildNumber = scope?.childBuildNumber
  const unreviewedCount = useMemo(
    () =>
      result
        ? countUnreviewedFailures(
            result.failures ?? [],
            result.child_job_analyses ?? [],
            reviews,
            childJobName ? { childJobName, childBuildNumber } : undefined,
          )
        : 0,
    [result, reviews, childJobName, childBuildNumber],
  )
  const allowUnreviewedPush = exporter === 'reportportal' ? rpAllowUnreviewedPush : ddAllowUnreviewedPush
  return { unreviewedCount, allowUnreviewedPush }
}
