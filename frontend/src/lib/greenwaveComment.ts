import type { FailureAnalysis } from '@/types'
import { reviewKey } from '@/lib/reviewKey'

/**
 * Deterministic proposal for the Greenwave waiver comment. The text is stored
 * in an external gating system (ResultsDB/WaiverDB), so it must be stable,
 * reviewable and short: no timestamps, usernames or per-test names.
 *
 * The classification counted per failure is the EFFECTIVE one — a user
 * override wins over the AI's classification, exactly as the report page
 * renders it (``classifications[key] ?? analysis.classification``). A comment
 * claiming the AI's label after a human overruled it would misdescribe the
 * gating write it accompanies.
 */
export function proposeWaiverComment({ jobName, buildNumber, failures, childJobName, childBuildNumber, classifications }: {
  jobName: string
  buildNumber?: number | string | null
  failures: FailureAnalysis[]
  /** Parent scope when these failures belong to a child job. */
  childJobName?: string
  childBuildNumber?: number
  /** Effective classification overrides, keyed by review key. */
  classifications?: Record<string, string>
}): string {
  const counts = new Map<string, number>()
  let unclassified = 0
  for (const f of failures) {
    const effective =
      classifications?.[reviewKey(f.test_name, childJobName, childBuildNumber)] ??
      f.analysis?.classification
    const classification = effective?.trim()
    if (classification) {
      const key = classification.toUpperCase()
      counts.set(key, (counts.get(key) ?? 0) + 1)
    } else {
      unclassified++
    }
  }

  const total = failures.length
  let text = `rootcoz: ${total} failure${total !== 1 ? 's' : ''} reviewed for ${jobName}`
  if (buildNumber !== null && buildNumber !== undefined && buildNumber !== '') {
    text += ` #${buildNumber}`
  }
  if (counts.size > 0) {
    // Deterministic order: count desc, then name asc (plain comparison —
    // localeCompare results vary across ICU builds).
    const parts = [...counts.entries()]
      .sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
      .map(([name, count]) => `${name}: ${count}`)
    if (unclassified > 0) parts.push(`unclassified: ${unclassified}`)
    text += ` — ${parts.join(', ')}`
  }
  return text
}
