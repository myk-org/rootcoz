import type { PeerRound } from '@/types'

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
