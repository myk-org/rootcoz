import { describe, expect, it } from 'vitest'
import { buildProviderOptions, normalizeProvider } from '@/lib/aiProviders'

describe('normalizeProvider', () => {
  it('maps legacy *-cli aliases to canonical names', () => {
    expect(normalizeProvider('cursor-cli')).toBe('cli-cursor')
    expect(normalizeProvider('claude-cli')).toBe('cli-claude')
    expect(normalizeProvider('CURSOR')).toBe('cursor')
  })
})

describe('buildProviderOptions', () => {
  it('includes exact catalog provider ids, including OpenAI and CLI Cursor', () => {
    expect(buildProviderOptions([]).map((o) => o.value)).toEqual([])
    expect(buildProviderOptions(['openai', 'cli-cursor']).map((o) => o.value)).toEqual([
      'cli-cursor',
      'openai',
    ])
  })

  it('normalizes legacy current selection', () => {
    expect(
      buildProviderOptions([], ['cursor-cli']).map((o) => o.value),
    ).toEqual(['cli-cursor'])
  })

  it('keeps current selection even without models', () => {
    expect(
      buildProviderOptions(['cursor'], ['claude']).map((o) => o.value),
    ).toEqual(['claude', 'cursor'])
  })

  it('keeps auth-failed providers when passed as currentValues', () => {
    // useProviderOptions merges status-failed ids into currentValues
    expect(
      buildProviderOptions(['claude'], ['cursor']).map((o) => o.value),
    ).toEqual(['claude', 'cursor'])
  })
})
