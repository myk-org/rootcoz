import type { ChildJobAnalysis } from '@/types'
import { groupFailures } from '@/lib/grouping'

/**
 * Build a URL-safe hash fragment identifier for a child job.
 * When parentHashId is provided, it prefixes the id to prevent collisions
 * in recursive (nested) child job trees.
 * Format: "child-<jobname>-<buildnumber>" or "<parentHashId>--child-<jobname>-<buildnumber>"
 */
export function childJobHashId(jobName: string, buildNumber: number, parentHashId?: string): string {
  const segment = `child-${encodeURIComponent(jobName)}-${buildNumber}`
  return parentHashId ? `${parentHashId}--${segment}` : segment
}

/**
 * Recursively collect all expand/collapse session-state keys for a child-job
 * tree.  Used by both `ReportPage` (expand/collapse all) and `ChildJobSection`
 * (per-section keys) so the key contract is defined in one place.
 */
export function collectChildExpandKeys(
  children: ChildJobAnalysis[],
  resultJobId: string,
  parentHashId?: string,
): string[] {
  const keys: string[] = []
  for (const child of children ?? []) {
    const hashId = childJobHashId(child.job_name, child.build_number, parentHashId)
    keys.push(`jji-expand-${resultJobId}-${hashId}`)
    const childGroups = groupFailures(child.failures ?? [], `child-${hashId}`)
    for (const g of childGroups) {
      keys.push(`jji-expand-${resultJobId}-${g.id}`)
    }
    keys.push(...collectChildExpandKeys(child.failed_children ?? [], resultJobId, hashId))
  }
  return keys
}
