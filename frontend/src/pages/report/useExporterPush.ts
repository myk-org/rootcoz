import { useCallback, useState } from 'react'
import { api, extractApiDetail } from '@/lib/api'
import { pushScopeQuery, type PushScope } from '@/lib/pushScopes'
import type { ExporterPushResult } from '@/types'

/** True only for a fully successful push: the request returned a body,
 * the exporter reported success, and no per-failure errors came back.
 * A hosting dialog may close on this; partial errors or a failed
 * roundtrip must stay visible. Shared by every exporter caller so the
 * success criterion is defined once.
 */
export function pushFullySucceeded(result: ExporterPushResult | null): boolean {
  return !!result?.success && (result.errors?.length ?? 0) === 0
}

/** Extra push inputs the generic exporter endpoint accepts beyond the scope. */
export interface ExporterPushExtras {
  subject_identifier?: string
  waiver_comment?: string
}

/**
 * POST /results/{job_id}/push/{plugin} — the ONLY place that builds this
 * request, for every exporter (reportportal, datadog, greenwave, …).
 * Scope params come from `pushScopeQuery`, so every caller gets an
 * identical URL. Extras are only sent when non-empty; the server requires
 * `subject_identifier` only when waivers are enabled, so an empty value is
 * never sent. `waiver_comment` is not stored verbatim: the server embeds it
 * in the composed WaiverDB comment (pusher name, classification, analysis
 * details, reviewer) and ResultsDB stores only the outcome per failure.
 *
 * The response is exporter-specific: pass the plugin's result type
 * (`ReportPortalPushResult`, `DatadogPushResult`, …); `ExporterPushResult`
 * is the default.
 */
export function pushToExporter<T = ExporterPushResult>(
  jobId: string,
  plugin: string,
  scope: PushScope = {},
  extra?: ExporterPushExtras,
): Promise<T> {
  const params = new URLSearchParams(pushScopeQuery(scope).slice(1))
  const subject = extra?.subject_identifier?.trim()
  if (subject) params.set('subject_identifier', subject)
  const comment = extra?.waiver_comment?.trim()
  if (comment) params.set('waiver_comment', comment)
  const qs = params.toString()
  return api.post<T>(`/results/${jobId}/push/${plugin}${qs ? `?${qs}` : ''}`)
}

/**
 * Push-state wrapper around `pushToExporter` for components that render
 * the outcome themselves (e.g. the Greenwave push form). Callers that need
 * the raw rejection (gate messages) call `pushToExporter` directly.
 */
export function useExporterPush({ jobId, plugin, childJobName, childBuildNumber }: {
  jobId: string
  plugin: string
  childJobName?: string
  childBuildNumber?: number
}) {
  const [pushing, setPushing] = useState(false)
  const [result, setResult] = useState<ExporterPushResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const push = useCallback(async (extra?: ExporterPushExtras) => {
    setPushing(true)
    setError(null)
    setResult(null)
    try {
      const res = await pushToExporter(jobId, plugin, { childJobName, childBuildNumber }, extra)
      setResult(res)
      return res
    } catch (err) {
      // A 422 prerequisite message (e.g. waivers enabled but no subject
      // identifier) must reach the UI as the server's `detail` text.
      setError(extractApiDetail(err) ?? 'Push failed')
      return null
    } finally {
      setPushing(false)
    }
  }, [jobId, plugin, childJobName, childBuildNumber])

  const reset = useCallback(() => {
    setResult(null)
    setError(null)
  }, [])

  return { push, pushing, result, error, reset }
}
