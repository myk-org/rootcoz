import { describe, expect, it } from 'vitest'
import { buildProviderOptions, normalizeProvider } from '@/lib/aiProviders'

describe('normalizeProvider', () => {
  it('lowercases and trims provider identifiers without alias rewriting', () => {
    expect(normalizeProvider('  OpenAI  ')).toBe('openai')
    expect(normalizeProvider('Vertex-Claude')).toBe('vertex-claude')
    expect(normalizeProvider('GEMINI')).toBe('gemini')
  })
})

describe('buildProviderOptions', () => {
  it('includes exact catalog provider ids sorted alphabetically', () => {
    expect(buildProviderOptions([]).map((o) => o.value)).toEqual([])
    expect(buildProviderOptions(['openai', 'vertex-claude']).map((o) => o.value)).toEqual([
      'openai',
      'vertex-claude',
    ])
  })

  it('normalizes current selection casing and whitespace', () => {
    expect(
      buildProviderOptions([], ['  OpenAI  ']).map((o) => o.value),
    ).toEqual(['openai'])
  })

  it('keeps current selection even without models', () => {
    expect(
      buildProviderOptions(['gemini'], ['claude']).map((o) => o.value),
    ).toEqual(['claude', 'gemini'])
  })
})
