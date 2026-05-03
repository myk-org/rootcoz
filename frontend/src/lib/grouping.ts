import type { FailureAnalysis, GroupedFailure } from '@/types'

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

/** Compute grouping key — matches Python _grouping_key(). */
export function groupingKey(failure: FailureAnalysis): string {
  return failure.error_signature || `unique-${failure.test_name}`
}

/** Group failures by error signature, preserving order. */
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
    groups.push({
      signature,
      tests,
      count: tests.length,
      id: `${idPrefix}-${encodeURIComponent(signature)}`,
    })
  }
  return groups
}
