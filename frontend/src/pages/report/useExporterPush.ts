import { useCallback, useState } from 'react'
import { api, extractApiDetail } from '@/lib/api'
import type { ExporterPushResult } from '@/types'

/**
 * True only for a fully successful push: the request returned a body,
 * the exporter reported success, and no per-failure errors came back.
 * A hosting dialog may close on this; partial errors or a failed
 * roundtrip must stay visible. Shared by every exporter caller so the
 * success criterion is defined once.
 */
export function pushFullySucceeded(result: ExporterPushResult | null): boolean {
  return !!result?.success && (result.errors?.length ?? 0) === 0
}

/**
 * Shared push hook for the generic exporter endpoint
 * `POST /results/{job_id}/push/{plugin}` — the ONLY place that builds this
 * request. Both push buttons and the all-reviewed prompt go through it.
 *
 * Extra params are only sent when non-empty; the server requires
 * `subject_identifier` only when waivers are enabled, so an empty value is
 * never sent.
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

  const push = useCallback(async (extra?: { subject_identifier?: string; waiver_comment?: string }) => {
    setPushing(true)
    setError(null)
    setResult(null)
    try {
      const params = new URLSearchParams()
      if (childJobName) params.set('child_job_name', childJobName)
      if (childBuildNumber != null) params.set('child_build_number', String(childBuildNumber))
      const subject = extra?.subject_identifier?.trim()
      if (subject) params.set('subject_identifier', subject)
      const comment = extra?.waiver_comment?.trim()
      if (comment) params.set('waiver_comment', comment)
      const qs = params.toString()
      const res = await api.post<ExporterPushResult>(`/results/${jobId}/push/${plugin}${qs ? `?${qs}` : ''}`)
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
