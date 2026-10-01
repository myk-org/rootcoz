import { walkChildTree } from './failureKeys'
import type { AnalysisResult, FailureAnalysis, GroupedFailure } from '@/types'

/** Check whether a comment belongs to the given test-group scope. */
export function isCommentInScope(
  comment: { test_name: string; child_job_name?: string; child_build_number?: number },
  groupTestNames: string[] | Set<string>,
  childJobName?: string,
  childBuildNumber?: number,
): boolean {
  const names = groupTestNames instanceof Set ? groupTestNames : new Set(groupTestNames)
  if (!names.has(comment.test_name)) return false
  const scopedChildJobName = childJobName ?? ''
  const scopedChildBuildNumber = childBuildNumber ?? 0
  const commentChildJobName = comment.child_job_name ?? ''
  const commentChildBuildNumber = comment.child_build_number ?? 0
  if (scopedChildJobName) {
    return (
      commentChildJobName === scopedChildJobName &&
      (commentChildBuildNumber === 0 || commentChildBuildNumber === scopedChildBuildNumber)
    )
  }
  return commentChildJobName === ''
}

/** Resolve the signature a failure is keyed by.
 *
 *  `error_signature` is a frozen legacy anchor; `error_signature_v2` holds
 *  the current-rules hash. Grouping must key on the resolved value
 *  (`error_signature_v2 || error_signature`) because siblings inside one group
 *  can carry DIFFERENT legacy `error_signature` values while sharing the
 *  group's `error_signature_v2`. The legacy value is never overwritten. */
export function resolvedSignature(failure: FailureAnalysis): string {
  return failure.error_signature_v2 || failure.error_signature || ''
}

/** Compute grouping key — matches Python _grouping_key(). */
export function groupingKey(failure: FailureAnalysis): string {
  return resolvedSignature(failure) || `unique-${failure.test_name}`
}

/** Group failures by error signature, preserving order.
 *
 *  The group ``id`` uses the first failure's UUID (``failure.id``) when
 *  available so that URL anchors and sessionStorage keys remain stable
 *  across re-analyses.  Falls back to the signature-based id for
 *  legacy data without UUIDs.
 */
export function groupFailures(
  failures: FailureAnalysis[],
  prefix = '',
): GroupedFailure[] {
  const groupMap = new Map<string, FailureAnalysis[]>()
  const idPrefix = prefix || 'group'

  for (const f of failures ?? []) {
    const key = groupingKey(f)
    if (!groupMap.has(key)) {
      groupMap.set(key, [])
    }
    groupMap.get(key)!.push(f)
  }

  const groups: GroupedFailure[] = []
  for (const [signature, tests] of groupMap) {
    // Prefer the first failure's stable UUID for the group id
    const stableId = tests[0]?.id
      ? `${idPrefix}-${tests[0].id}`
      : `${idPrefix}-${encodeURIComponent(signature)}`
    groups.push({
      signature,
      tests,
      count: tests.length,
      id: stableId,
    })
  }
  return groups
}

/** Failure groups of one child-job scope — the top-level failures when no child job
 *  is named, otherwise every failure list of that job anywhere in the tree. */
export function scopedGroups(
  result: Pick<AnalysisResult, 'failures' | 'child_job_analyses'>,
  childJobName?: string,
  childBuildNumber?: number,
): GroupedFailure[] {
  const name = childJobName ?? ''
  const build = childBuildNumber ?? 0
  const scoped: FailureAnalysis[] = []
  walkChildTree(result.failures ?? [], result.child_job_analyses ?? [], (fs, jobName, jobBuild) => {
    if ((jobName ?? '') === name && (jobBuild ?? 0) === build) scoped.push(...(fs as FailureAnalysis[]))
  })
  return groupFailures(scoped)
}
