import { describe, it, expect } from 'vitest'
import { getPeerAiLabels } from '../PeerAnalysisSummary'
import type { PeerDebate } from '@/types'

function debate(ai_configs: PeerDebate['ai_configs']): { debate: PeerDebate } {
  return {
    debate: {
      consensus_reached: true,
      rounds_used: 1,
      max_rounds: 1,
      rounds: [],
      ai_configs,
    },
  }
}

describe('getPeerAiLabels', () => {
  const main = { ai_provider: 'cursor', ai_model: 'cursor:cursor-grok-4.5-high-fast' }
  const peerClaude = { ai_provider: 'claude', ai_model: 'claude-opus-4-6-1m' }
  const peerComposer = { ai_provider: 'cursor', ai_model: 'cursor:composer-2.5-fast' }

  it('drops ai_configs[0] (main) and keeps peers', () => {
    const labels = getPeerAiLabels([debate([main, peerClaude, peerComposer])])
    expect(labels).toEqual([
      'claude/claude-opus-4-6-1m',
      'cursor/cursor:composer-2.5-fast',
    ])
  })

  it('keeps a peer that shares the main provider/model', () => {
    const labels = getPeerAiLabels([debate([main, main])])
    expect(labels).toEqual(['cursor/cursor:cursor-grok-4.5-high-fast'])
  })

  it('dedupes peer labels across debates', () => {
    const labels = getPeerAiLabels([
      debate([main, peerClaude]),
      debate([main, peerClaude, peerComposer]),
    ])
    expect(labels).toEqual([
      'claude/claude-opus-4-6-1m',
      'cursor/cursor:composer-2.5-fast',
    ])
  })

  it('returns empty when only main is present', () => {
    expect(getPeerAiLabels([debate([main])])).toEqual([])
  })
})
