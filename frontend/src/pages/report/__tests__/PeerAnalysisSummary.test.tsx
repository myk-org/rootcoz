import { describe, it, expect } from 'vitest'
import { getPeerAiLabels } from '../peerAiLabels'
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
  const main = { ai_provider: 'openai', ai_model: 'gpt-5' }
  const peerClaude = { ai_provider: 'claude', ai_model: 'claude-opus-4-6-1m' }
  const peerGemini = { ai_provider: 'gemini', ai_model: 'gemini-2.5-pro' }

  it('drops ai_configs[0] (main) and keeps peers', () => {
    const labels = getPeerAiLabels([debate([main, peerClaude, peerGemini])])
    expect(labels).toEqual([
      'Claude/claude-opus-4-6-1m',
      'Gemini/gemini-2.5-pro',
    ])
  })

  it('keeps a peer that shares the main provider/model', () => {
    const labels = getPeerAiLabels([debate([main, main])])
    expect(labels).toEqual(['OpenAI/gpt-5'])
  })

  it('dedupes peer labels across debates', () => {
    const labels = getPeerAiLabels([
      debate([main, peerClaude]),
      debate([main, peerClaude, peerGemini]),
    ])
    expect(labels).toEqual([
      'Claude/claude-opus-4-6-1m',
      'Gemini/gemini-2.5-pro',
    ])
  })

  it('maps internal sidecar provider ids to public labels', () => {
    const labels = getPeerAiLabels([
      debate([
        main,
        { ai_provider: 'google-vertex-claude', ai_model: 'claude-sonnet-4-6' },
        { ai_provider: 'google-vertex', ai_model: 'gemini-2.5-pro' },
      ]),
    ])
    expect(labels).toEqual([
      'Claude/claude-sonnet-4-6',
      'Gemini/gemini-2.5-pro',
    ])
  })

  it('keeps distinct google and google-vertex peers visible and restores custom provider casing', () => {
    const labels = getPeerAiLabels(
      [
        debate([
          main,
          { ai_provider: 'google', ai_model: 'gemini-2.5-pro' },
          { ai_provider: 'google-vertex', ai_model: 'gemini-2.5-pro' },
          { ai_provider: 'acmeai', ai_model: 'model-1' },
        ]),
      ],
      { acmeai: 'AcmeAI' },
    )
    expect(labels).toEqual([
      'AcmeAI/model-1',
      'Gemini (API)/gemini-2.5-pro',
      'Gemini (Vertex)/gemini-2.5-pro',
    ])
  })

  it('returns empty when only main is present', () => {
    expect(getPeerAiLabels([debate([main])])).toEqual([])
  })
})
