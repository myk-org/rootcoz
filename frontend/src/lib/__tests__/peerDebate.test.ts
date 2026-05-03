import { describe, it, expect } from 'vitest'
import { groupPeerRounds } from '../peerDebate'
import type { PeerRound } from '@/types'

function makeRound(round: number, role: 'orchestrator' | 'peer', provider = 'claude'): PeerRound {
  return {
    round,
    ai_provider: provider,
    ai_model: 'opus',
    role,
    classification: 'CODE ISSUE',
    details: 'some details',
    agrees_with_orchestrator: role === 'orchestrator' ? null : true,
  }
}

describe('groupPeerRounds', () => {
  it('returns empty array for empty rounds', () => {
    expect(groupPeerRounds([])).toEqual([])
  })

  it('groups rounds by round number', () => {
    const rounds = [
      makeRound(1, 'orchestrator'),
      makeRound(1, 'peer', 'gemini'),
      makeRound(2, 'orchestrator'),
      makeRound(2, 'peer', 'gemini'),
    ]

    const grouped = groupPeerRounds(rounds)
    expect(grouped).toHaveLength(2)
    expect(grouped[0].round).toBe(1)
    expect(grouped[0].entries).toHaveLength(2)
    expect(grouped[1].round).toBe(2)
    expect(grouped[1].entries).toHaveLength(2)
  })

  it('sorts groups by round number ascending', () => {
    const rounds = [
      makeRound(3, 'orchestrator'),
      makeRound(1, 'peer'),
      makeRound(2, 'orchestrator'),
    ]

    const grouped = groupPeerRounds(rounds)
    expect(grouped.map((g) => g.round)).toEqual([1, 2, 3])
  })

  it('preserves entry order within each round', () => {
    const rounds = [
      makeRound(1, 'orchestrator', 'claude'),
      makeRound(1, 'peer', 'gemini'),
      makeRound(1, 'peer', 'cursor'),
    ]

    const grouped = groupPeerRounds(rounds)
    expect(grouped).toHaveLength(1)
    expect(grouped[0].entries[0].ai_provider).toBe('claude')
    expect(grouped[0].entries[1].ai_provider).toBe('gemini')
    expect(grouped[0].entries[2].ai_provider).toBe('cursor')
  })

  it('handles single round with single entry', () => {
    const rounds = [makeRound(1, 'orchestrator')]
    const grouped = groupPeerRounds(rounds)
    expect(grouped).toHaveLength(1)
    expect(grouped[0].round).toBe(1)
    expect(grouped[0].entries).toHaveLength(1)
  })
})
