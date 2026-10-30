import { reviewKey } from '@/lib/reviewKey'
import type { ChildJobAnalysis, ReviewState } from '@/types'

/** Walk the child-job tree once, calling `visitor` at each node.
 *  Centralises the recursion so every consumer stays in sync. */
export function walkChildTree(
  failures: { test_name: string }[],
  children: ChildJobAnalysis[],
  visitor: (failures: { test_name: string }[], parentJobName?: string, parentBuildNumber?: number) => void,
  parentJobName?: string,
  parentBuildNumber?: number,
): void {
  visitor(failures ?? [], parentJobName, parentBuildNumber)
  for (const child of children ?? []) {
    walkChildTree(child.failures ?? [], child.failed_children ?? [], visitor, child.job_name, child.build_number)
  }
}

/** Recursively collect all review keys from failures + nested children. */
export function collectAllTestKeys(
  failures: { test_name: string }[],
  children: ChildJobAnalysis[],
  parentJobName?: string,
  parentBuildNumber?: number,
): string[] {
  const keys: string[] = []
  walkChildTree(failures, children, (nodeFailures, jobName, buildNumber) => {
    for (const f of nodeFailures) {
      keys.push(reviewKey(f.test_name, jobName, buildNumber))
    }
  }, parentJobName, parentBuildNumber)
  return keys
}

/** Recursively count all failures including nested children. */
export function countAllFailures(failures: { test_name: string }[], children: ChildJobAnalysis[]): number {
  let count = 0
  walkChildTree(failures, children, (nodeFailures) => {
    count += nodeFailures.length
  })
  return count
}

/** How many of the given review keys are marked reviewed. */
export function reviewedKeyCount(
  keys: string[],
  reviews: Record<string, ReviewState>,
): number {
  return keys.filter((k) => reviews[k]?.reviewed).length
}

/** How many of the given review keys are NOT yet reviewed. */
export function unreviewedKeyCount(
  keys: string[],
  reviews: Record<string, ReviewState>,
): number {
  return keys.length - reviewedKeyCount(keys, reviews)
}

/** Resolve whether a single failure is reviewed, mirroring the server's
 *  `resolve_failure_review` precedence: for a child-scoped test the exact-build
 *  key wins, with the wildcard (build 0) key as fallback — a review stored
 *  without a build number covers any build of that child job. A child-scoped
 *  test never matches a bare top-level review and vice versa. */
function isFailureReviewed(
  testName: string,
  childJobName: string | undefined,
  childBuildNumber: number | undefined,
  reviews: Record<string, ReviewState>,
): boolean {
  if (!childJobName) return !!reviews[testName]?.reviewed
  if (reviews[reviewKey(testName, childJobName, childBuildNumber)]?.reviewed) return true
  // Build 0 IS the wildcard key — the lookup above already covered it.
  if (childBuildNumber === 0) return false
  return !!reviews[reviewKey(testName, childJobName, 0)]?.reviewed
}

/** Job-wide unreviewed failure count (parent + nested children), resolving
 *  each child failure through the same exact-then-wildcard precedence the
 *  server's push gate uses (`resolve_failure_review`). */
/** Find a child node by (job name, build number), searching nested children. */
export function findChildNode(
  children: ChildJobAnalysis[],
  jobName: string,
  buildNumber?: number,
): ChildJobAnalysis | undefined {
  for (const child of children ?? []) {
    if (child.job_name === jobName && child.build_number === buildNumber) return child
    const found = findChildNode(child.failed_children ?? [], jobName, buildNumber)
    if (found) return found
  }
  return undefined
}

/** Job-wide unreviewed failure count (parent + nested children), resolving
 *  each child failure through the same exact-then-wildcard precedence the
 *  server's push gate uses (`resolve_failure_review`).
 *
 *  With a `scope`, only that subtree is counted — the server gates a push
 *  against exactly what it exports, so a fully reviewed child must not show
 *  "unreviewed failures" because a sibling or the parent is not reviewed
 *  yet. */
export function countUnreviewedFailures(
  failures: { test_name: string }[],
  children: ChildJobAnalysis[],
  reviews: Record<string, ReviewState>,
  scope?: { childJobName?: string; childBuildNumber?: number },
): number {
  const node = scope?.childJobName
    ? findChildNode(children, scope.childJobName, scope.childBuildNumber)
    : undefined
  let unreviewed = 0
  walkChildTree(
    node ? node.failures ?? [] : failures,
    node ? node.failed_children ?? [] : children,
    (nodeFailures, jobName, buildNumber) => {
      for (const f of nodeFailures) {
        if (!isFailureReviewed(f.test_name, jobName, buildNumber, reviews)) unreviewed += 1
      }
    },
    node?.job_name,
    node?.build_number,
  )
  return unreviewed
}
