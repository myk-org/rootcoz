import { walkChildTree } from '@/lib/failureKeys'
import type { ChildJobAnalysis } from '@/types'

/** A push target scope: the parent job (no child fields) or one child job. */
export interface PushScope {
  childJobName?: string
  childBuildNumber?: number
}

/**
 * Collect every scope that has failures: the parent job (when it has
 * failures) plus each child job node in the tree that has failures.
 *
 * Pushes are scoped per node so a pipeline whose failures live only in
 * child jobs still pushes every reviewed scope — a plain parent push
 * would send an empty failure list.
 */
export function collectPushScopes(
  failures: { test_name: string }[],
  children: ChildJobAnalysis[],
): PushScope[] {
  const scopes: PushScope[] = []
  const seen = new Set<string>()
  walkChildTree(failures, children, (nodeFailures, jobName, buildNumber) => {
    if (nodeFailures.length === 0) return
    // Two child nodes can share the same (job name, build number) — e.g. the
    // same child reported under two parents. They would produce identical
    // push requests that the backend resolves to the first node, so an
    // identical scope must never be sent twice.
    const key = JSON.stringify([jobName ?? null, buildNumber ?? null])
    if (seen.has(key)) return
    seen.add(key)
    if (jobName === undefined) scopes.push({})
    else scopes.push({ childJobName: jobName, childBuildNumber: buildNumber })
  })
  return scopes
}

/**
 * Query string (with leading '?' when non-empty) for the child-scoped push
 * endpoints (`/push-reportportal`, `/push/{plugin}`). Shared by the push
 * buttons and the all-reviewed prompt so every caller builds identical URLs.
 */
export function pushScopeQuery({ childJobName, childBuildNumber }: PushScope): string {
  const params = new URLSearchParams()
  if (childJobName) params.set('child_job_name', childJobName)
  if (childBuildNumber != null) params.set('child_build_number', String(childBuildNumber))
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}
