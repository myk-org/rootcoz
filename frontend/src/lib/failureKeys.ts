import { reviewKey } from '@/lib/reviewKey'
import type { ChildJobAnalysis } from '@/types'

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
