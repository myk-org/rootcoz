import type { AnalysisResult, PeerRound } from '@/types'

export interface GroupedRound {
  round: number
  entries: PeerRound[]
}

/**
 * Group peer rounds by round number, sorted ascending.
 *
 * Collects unique round numbers from the given rounds array,
 * sorts them, and returns each round with its filtered entries.
 */
export function groupPeerRounds(rounds: PeerRound[]): GroupedRound[] {
  const roundNumbers = [...new Set(rounds.map((r) => r.round))].sort((a, b) => a - b)
  return roundNumbers.map((round) => ({
    round,
    entries: rounds.filter((r) => r.round === round),
  }))
}

/** Unique peer provider/model pairs the completed analysis actually used
 *  (role 'peer' entries across all debates, top-level and child jobs).
 *  Used to replay resolved peer configs when request params omitted them. */
export function resolvedPeerConfigs(result: AnalysisResult): Array<{ ai_provider: string; ai_model: string }> {
  const pairs: Array<{ ai_provider: string; ai_model: string }> = []
  const seen = new Set<string>()
  const collect = (failures: AnalysisResult['failures']): void => {
    for (const f of failures) {
      for (const r of f.peer_debate?.rounds ?? []) {
        if (r.role !== 'peer' || !r.ai_provider || !r.ai_model) continue
        const key = `${r.ai_provider}::${r.ai_model}`
        if (seen.has(key)) continue
        seen.add(key)
        pairs.push({ ai_provider: r.ai_provider, ai_model: r.ai_model })
      }
    }
  }
  collect(result.failures ?? [])
  for (const child of result.child_job_analyses ?? []) collect(child.failures ?? [])
  return pairs
}
