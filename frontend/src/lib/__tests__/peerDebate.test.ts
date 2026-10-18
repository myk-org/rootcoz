import { describe, it, expect } from 'vitest'
import { groupPeerRounds, resolvedPeerConfigs } from '../peerDebate'
import type { PeerRound, AnalysisResult } from '@/types'

function makeRound(round: number, role: 'orchestrator' | 'peer', provider = 'claude'): PeerRound {
  return {
    round,
    ai_provider: provider,
    ai_model: 'test-model',
    role,
    classification: 'CODE ISSUE',
    pattern: '',
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

describe('resolvedPeerConfigs', () => {
  const failureWithDebate = (provider: string): object => ({
    test_name: `test-${provider}`,
    peer_debate: {
      rounds: [makeRound(1, 'orchestrator', 'claude'), makeRound(1, 'peer', provider)],
    },
  })

  it('collects unique peer pairs from top-level failures', () => {
    const result = {
      failures: [failureWithDebate('openai'), failureWithDebate('openai')],
    } as unknown as AnalysisResult
    expect(resolvedPeerConfigs(result)).toEqual([
      { ai_provider: 'openai', ai_model: 'test-model' },
    ])
  })

  it('collects peer pairs from nested failed_children at any depth', () => {
    const result = {
      failures: [],
      child_job_analyses: [
        {
          job_name: 'child',
          failures: [],
          failed_children: [
            {
              job_name: 'grandchild',
              failures: [failureWithDebate('gemini')],
              failed_children: [
                {
                  job_name: 'great-grandchild',
                  failures: [failureWithDebate('cursor')],
                  failed_children: [],
                },
              ],
            },
          ],
        },
      ],
    } as unknown as AnalysisResult
    expect(resolvedPeerConfigs(result)).toEqual([
      { ai_provider: 'gemini', ai_model: 'test-model' },
      { ai_provider: 'cursor', ai_model: 'test-model' },
    ])
  })

  it('returns empty when no debates exist', () => {
    const result = { failures: [{ test_name: 'x' }] } as unknown as AnalysisResult
    expect(resolvedPeerConfigs(result)).toEqual([])
  })
})
