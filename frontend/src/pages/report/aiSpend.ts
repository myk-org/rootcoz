import type { AnalysisResult, TokenUsageEntry, TokenUsageSummary } from '@/types'
import { walkChildTree } from '@/lib/failureKeys'
import { groupingKey } from '@/lib/grouping'
import { groupPeerRounds } from '@/lib/peerDebate'

/**
 * Job-level AI spend attribution for the Report page.
 *
 * Everything here is derived from stored data with explicit denominators:
 * a `null` cost means *unavailable*, never zero. Cache tokens are reported
 * apart from input/output and are never added to the billed `totalTokens`.
 */

export interface StageUsage {
  callType: string
  calls: number
  inputTokens: number
  outputTokens: number
  cacheReadTokens: number
  cacheWriteTokens: number
  /** Input + output only — cache tokens are not billed the same way. */
  totalTokens: number
  /** Null when any call in the stage has no recorded cost. */
  costUsd: number | null
  /** Calls with a recorded `success: false` outcome. */
  failedCalls: number
  /** False when any call predates outcome tracking, so `failedCalls` is a lower bound. */
  outcomeKnown: boolean
  /** Cost of failed calls; null when any failed call has no recorded cost. */
  failedCostUsd: number | null
  /** True when a call's recorded cost covers only some turns (a lower bound). */
  partial: boolean
}

/** Sum costs, treating any unknown cost as making the total unavailable. */
function sumKnownCost(calls: TokenUsageEntry[]): number | null {
  let total = 0
  for (const call of calls) {
    if (call.cost_usd == null) return null
    total += call.cost_usd
  }
  return total
}

/** Breakdown of the job summary by recorded `call_type`. */
export function stageBreakdown(usage: TokenUsageSummary | null | undefined): StageUsage[] {
  const byType = new Map<string, TokenUsageEntry[]>()
  for (const call of usage?.calls ?? []) {
    const bucket = byType.get(call.call_type)
    if (bucket) bucket.push(call)
    else byType.set(call.call_type, [call])
  }
  return [...byType.entries()].map(([callType, calls]) => {
    const failed = calls.filter(call => call.success === false)
    return {
      callType,
      calls: calls.length,
      inputTokens: calls.reduce((sum, c) => sum + c.input_tokens, 0),
      outputTokens: calls.reduce((sum, c) => sum + c.output_tokens, 0),
      cacheReadTokens: calls.reduce((sum, c) => sum + c.cache_read_tokens, 0),
      cacheWriteTokens: calls.reduce((sum, c) => sum + c.cache_write_tokens, 0),
      totalTokens: calls.reduce((sum, c) => sum + c.total_tokens, 0),
      costUsd: sumKnownCost(calls),
      failedCalls: failed.length,
      outcomeKnown: calls.every(call => call.success != null),
      failedCostUsd: failed.length > 0 ? sumKnownCost(failed) : null,
      partial: calls.some(call => call.cost_partial),
    }
  })
}

export interface PrimaryGroup {
  /** `childLabel::signature` — child scope plus signature, so name collisions stay distinct. */
  key: string
  childLabel: string
  signature: string
  testName: string
  /** Failures sharing this signature within this child scope. */
  failureCount: number
  /** Null when usage was withheld (ambiguous legacy rows) or never recorded. */
  usage: TokenUsageSummary | null
}

export interface PeerCallRow {
  round: number
  role: 'orchestrator' | 'peer'
  /** `provider/model`, or just the model when the provider is blank. */
  agent: string
  /** Stable per-round agent identity so same-model peers stay distinct. */
  agentLabel: string
  usage: TokenUsageEntry | null
}

export interface PeerGroup {
  key: string
  childLabel: string
  testName: string
  /** Other failures sharing this debate via the same signature. */
  siblingCount: number
  rounds: PeerCallRow[]
  /** Null when any attributable round cost is unknown. */
  costUsd: number | null
}

interface TreeNode {
  failures: AnalysisResult['failures']
  child_job_analyses: AnalysisResult['child_job_analyses']
}

/** Label identifying a child-job scope; empty string for the top-level job. */
function scopeLabel(jobName?: string, buildNumber?: number): string {
  if (!jobName) return ''
  return buildNumber ? `${jobName} #${buildNumber}` : jobName
}

type Failure = NonNullable<TreeNode['failures']>[number]

/** Walk unique failure groups, keyed by child scope + signature. */
function uniqueFailureGroups(result: TreeNode): { childLabel: string; signature: string; testName: string; failures: Failure[] }[] {
  const groups = new Map<string, { childLabel: string; signature: string; testName: string; failures: Failure[] }>()
  walkChildTree(result.failures ?? [], result.child_job_analyses ?? [], (failures, jobName, buildNumber) => {
    const childLabel = scopeLabel(jobName, buildNumber)
    for (const failure of failures as Failure[]) {
      // Key on the same resolved signature the backend attributes usage by, so
      // siblings that share a v2 signature never split into duplicate spend rows.
      const signature = groupingKey(failure)
      const key = `${childLabel}::${signature}`
      const existing = groups.get(key)
      if (existing) existing.failures.push(failure)
      else groups.set(key, { childLabel, signature, testName: failure.test_name, failures: [failure] })
    }
  })
  return [...groups.values()]
}

/** Primary usage per unique failure signature and child scope. */
export function primaryGroups(result: TreeNode): PrimaryGroup[] {
  return uniqueFailureGroups(result).map(({ childLabel, signature, testName, failures }) => ({
    key: `${childLabel}::${signature}`,
    childLabel,
    signature,
    testName,
    failureCount: failures.length,
    // A re-analyzed failure carries that attempt's usage, not the group's original
    // primary calls. Without an unattempted failure the group cost stays unavailable
    // rather than being reported as primary spend.
    usage: failures.find(f => f.token_usage && !f.usage_attempt)?.token_usage ?? null,
  }))
}

/** Round 1's orchestrator deliberately reuses the primary call, so it has no own usage. */
function reusesPrimaryCall(round: PeerCallRow): boolean {
  return round.round === 1 && round.role === 'orchestrator'
}

/** Peer/revision usage per debate, by round and agent identity. */
export function peerGroups(result: TreeNode): PeerGroup[] {
  const debates: PeerGroup[] = []
  for (const { childLabel, signature, failures } of uniqueFailureGroups(result)) {
    // Failures sharing a signature can hold different current debates after one is
    // re-analyzed. `debate_id` identifies one execution; legacy results fall back to
    // serialized content, which can only merge debates that are genuinely identical.
    const byDebate = new Map<string, Failure[]>()
    for (const failure of failures) {
      if (!failure.peer_debate) continue
      const id = failure.peer_debate.debate_id ?? JSON.stringify(failure.peer_debate)
      const members = byDebate.get(id)
      if (members) members.push(failure)
      else byDebate.set(id, [failure])
    }
    let index = 0
    for (const members of byDebate.values()) {
      debates.push(buildPeerGroup(`${childLabel}::${signature}#${index++}`, childLabel, members))
    }
  }
  return debates
}

function buildPeerGroup(key: string, childLabel: string, members: Failure[]): PeerGroup {
  const debate = members[0].peer_debate!
  const peerCounters = new Map<number, number>()
  const rounds: PeerCallRow[] = groupPeerRounds(debate.rounds ?? []).flatMap(({ round, entries }) =>
    entries.map(entry => {
      const isPeer = entry.role === 'peer'
      // Number peers within the round: same-model peers stay distinguishable.
      const peerIndex = isPeer ? (peerCounters.get(round) ?? 0) + 1 : 0
      if (isPeer) peerCounters.set(round, peerIndex)
      // Name the model that was actually billed; the round's configured identity
      // can differ from what the call reported.
      const provider = entry.token_usage?.provider || entry.ai_provider
      const model = entry.token_usage?.model || entry.ai_model || 'unknown'
      return {
        round,
        role: entry.role,
        agent: provider ? `${provider}/${model}` : model,
        agentLabel: isPeer ? `Peer ${peerIndex}` : 'Main AI',
        usage: entry.token_usage ?? null,
      }
    }),
  )
  const used = rounds.filter(r => r.usage)
  // An attempted round without usage means part of the spend was never recorded;
  // a partial sum would read as a complete debate cost.
  const unattributed = rounds.some(r => !r.usage && !reusesPrimaryCall(r))
  return {
    key,
    childLabel,
    testName: members[0].test_name,
    siblingCount: members.length - 1,
    rounds,
    costUsd: used.length > 0 && !unattributed ? sumKnownCost(used.map(r => r.usage!)) : null,
  }
}

/** Failures carrying an analysis — the denominator for cost per failing test. */
export function analyzedFailingTests(result: TreeNode): number {
  let count = 0
  walkChildTree(result.failures ?? [], result.child_job_analyses ?? [], (failures) => {
    for (const failure of failures as Failure[]) {
      if (failure.analysis != null) count += 1
    }
  })
  return count
}

export interface SpendAverages {
  /** job total cost / analyzed failed tests; null when cost or failures are unknown. */
  costPerFailingTest: number | null
  /** Sum of attributable group costs / groups with a known cost. */
  costPerUniqueGroup: number | null
  /** Unique groups in the job, including those with no usable usage. */
  totalGroups: number
  /** Groups contributing to `costPerUniqueGroup` — smaller means partial data. */
  groupsWithKnownCost: number
  /** Peer cost / debated groups; null when no debate has a known cost. */
  peerCostPerDebatedGroup: number | null
  debatedGroups: number
  /** Debates contributing to `peerCostPerDebatedGroup` — the real denominator. */
  peersWithKnownCost: number
}

export function spendAverages(
  totalCostUsd: number | null | undefined,
  failedTests: number,
  groups: PrimaryGroup[],
  peers: PeerGroup[],
): SpendAverages {
  const withCost = groups.filter(g => g.usage?.total_cost_usd != null)
  const groupCost = withCost.reduce((sum, g) => sum + g.usage!.total_cost_usd!, 0)
  const peersWithCost = peers.filter(p => p.costUsd != null)
  const peerCost = peersWithCost.reduce((sum, p) => sum + p.costUsd!, 0)
  return {
    costPerFailingTest:
      totalCostUsd != null && failedTests > 0 ? totalCostUsd / failedTests : null,
    costPerUniqueGroup: withCost.length > 0 ? groupCost / withCost.length : null,
    totalGroups: groups.length,
    groupsWithKnownCost: withCost.length,
    peerCostPerDebatedGroup: peersWithCost.length > 0 ? peerCost / peersWithCost.length : null,
    debatedGroups: peers.length,
    peersWithKnownCost: peersWithCost.length,
  }
}
